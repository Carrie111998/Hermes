from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

import pytest

from scripts.canary import production_release_consumer_inventory as inventory
from scripts.canary import production_release_host_observer as observer


PREDECESSOR = "1" * 40
TARGET = "2" * 40
NOW_NS = 1_800_000_000_000_000_000
BOOT_ID = "12345678-1234-4234-8234-123456789abc"


def _release_root(revision: str) -> str:
    return (
        f"/opt/adventico-ai-platform/hermes-agent-releases/hermes-agent-{revision[:12]}"
    )


def _properties(
    spec: inventory.ConsumerSpec,
    *,
    revision: str = PREDECESSOR,
) -> dict[str, str]:
    values = {
        name: ""
        for name in observer._SYSTEMD_PROPERTIES  # noqa: SLF001
    }
    values.update({
        "Id": spec.name,
        "Names": spec.name,
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": ("running" if spec.kind == "service" else "waiting"),
        "UnitFileState": "enabled",
        "FragmentPath": spec.fragment_path,
        "DropInPaths": " ".join(spec.drop_in_paths),
        "NeedDaemonReload": "no",
        "MainPID": "0",
        "ControlPID": "0",
        "Triggers": " ".join(spec.triggers),
        "TriggeredBy": " ".join(spec.triggered_by),
    })
    if spec.executes_release:
        values["ExecStart"] = (
            f"{{ path={_release_root(revision)}/venv/bin/python3 ; "
            f"argv[]={_release_root(revision)}/venv/bin/python3 "
            f"{_release_root(revision)}/run_agent.py ; }}"
        )
        values["WorkingDirectory"] = _release_root(revision)
    return values


def _show_payload(blocks: Mapping[str, Mapping[str, str]]) -> bytes:
    rendered: list[str] = []
    for name in sorted(blocks):
        values = blocks[name]
        rendered.append(
            "\n".join(
                f"{property_name}={values[property_name]}"
                for property_name in observer._SYSTEMD_PROPERTIES  # noqa: SLF001
            )
        )
    return ("\n\n".join(rendered) + "\n").encode()


@dataclass
class _FakeRunner:
    unit_file_output: bytes
    loaded_output: bytes
    show_outputs: list[bytes]
    show_calls: int = 0

    def run(self, argv: tuple[str, ...]) -> observer.CommandResult:
        if argv == observer._LIST_UNIT_FILES_ARGV:  # noqa: SLF001
            return observer.CommandResult(stdout=self.unit_file_output)
        if argv == observer._LIST_UNITS_ARGV:  # noqa: SLF001
            return observer.CommandResult(stdout=self.loaded_output)
        if argv[: len(observer._SHOW_PREFIX)] == observer._SHOW_PREFIX:  # noqa: SLF001
            index = min(self.show_calls, len(self.show_outputs) - 1)
            self.show_calls += 1
            return observer.CommandResult(stdout=self.show_outputs[index])
        raise AssertionError(f"unexpected command: {argv!r}")


@dataclass
class _FakeUnitFiles:
    payloads: dict[str, bytes]
    swap_path: str | None = None
    reads: dict[str, int] | None = None

    def read(self, path: str) -> bytes:
        if self.reads is None:
            self.reads = {}
        self.reads[path] = self.reads.get(path, 0) + 1
        payload = self.payloads[path]
        if path == self.swap_path and self.reads[path] > 1:
            return payload + b"\n# swapped"
        return payload


class _FakeProc:
    def __init__(
        self,
        *,
        identity_snapshots: list[Mapping[int, int]] | None = None,
        processes: Mapping[int, observer.CollectedProcess] | None = None,
        fences: list[int] | None = None,
    ) -> None:
        self._identity_snapshots = identity_snapshots or [{}, {}, {}]
        self._processes = dict(processes or {})
        self._fences = fences or [77, 77, 77]
        self._identity_call = 0
        self._fence_call = 0

    def boot_id(self) -> str:
        return BOOT_ID

    def allocation_fence(self) -> int:
        index = min(self._fence_call, len(self._fences) - 1)
        self._fence_call += 1
        return self._fences[index]

    def identities(self) -> Mapping[int, int]:
        index = min(
            self._identity_call,
            len(self._identity_snapshots) - 1,
        )
        self._identity_call += 1
        return MappingProxyType(dict(self._identity_snapshots[index]))

    def observe(
        self,
        pid: int,
        start_time_ticks: int,
    ) -> observer.CollectedProcess:
        process = self._processes[pid]
        if process.start_time_ticks != start_time_ticks:
            raise observer.ProductionReleaseHostObserverError(
                "production_release_host_proc_pid_reused",
                str(pid),
            )
        return process


@dataclass
class _Harness:
    runner: _FakeRunner
    files: _FakeUnitFiles
    proc: _FakeProc
    blocks: dict[str, dict[str, str]]
    catalog: Mapping[str, inventory.ConsumerSpec]


def _harness(
    *,
    proc: _FakeProc | None = None,
    extra_blocks: Mapping[str, Mapping[str, str]] | None = None,
    extra_unit_names: tuple[str, ...] = (),
) -> _Harness:
    catalog = inventory.expected_consumer_catalog()
    blocks = {name: _properties(spec) for name, spec in catalog.items()}
    if extra_blocks:
        blocks.update({name: dict(values) for name, values in extra_blocks.items()})
    names = tuple(sorted((*catalog, *extra_unit_names)))
    unit_file_output = "".join(f"{name} enabled enabled\n" for name in names).encode()
    loaded_output = "".join(
        f"{name} loaded active running Test unit {name}\n" for name in names
    ).encode()
    payloads: dict[str, bytes] = {}
    for spec in catalog.values():
        service_type = (
            "notify"
            if spec.activation_class == inventory.ACTIVATION_CLASS_LONG_RUNNING
            else "oneshot"
        )
        payloads[spec.fragment_path] = (
            (
                "[Service]\n"
                f"Type={service_type}\n"
                f"ExecStart={_release_root(PREDECESSOR)}/venv/bin/python3 "
                f"{_release_root(PREDECESSOR)}/run_agent.py\n"
            ).encode()
            if spec.executes_release
            else b"[Unit]\nDescription=Trigger\n"
        )
        for path in spec.drop_in_paths:
            payloads[path] = b"[Service]\nNoNewPrivileges=yes\n"
    for name, values in (extra_blocks or {}).items():
        fragment = values["FragmentPath"]
        payloads[fragment] = (
            b"[Service]\n" + values.get("ExecStart", "").encode() + b"\n"
        )
        for path in values.get("DropInPaths", "").split():
            payloads[path] = b"[Service]\n"
    show = _show_payload(blocks)
    return _Harness(
        runner=_FakeRunner(
            unit_file_output=unit_file_output,
            loaded_output=loaded_output,
            show_outputs=[show, show],
        ),
        files=_FakeUnitFiles(payloads),
        proc=proc or _FakeProc(),
        blocks=blocks,
        catalog=catalog,
    )


def _observe(harness: _Harness) -> observer.HostObservationResult:
    return observer._observe_and_validate_release_host_for_test(
        phase=inventory.InventoryPhase.PREDECESSOR_ACTIVE,
        predecessor_revision=PREDECESSOR,
        target_revision=TARGET,
        command_runner=harness.runner,
        unit_file_reader=harness.files,
        proc_source=harness.proc,
        observed_at_unix_ns=NOW_NS,
    )


def _process(
    *,
    pid: int,
    start: int,
    unit: str | None,
    path: str,
) -> observer.CollectedProcess:
    return observer.CollectedProcess(
        observation=inventory.ProcessObservation(
            pid=pid,
            unit=unit,
            fields=MappingProxyType({
                "exe": path,
                "cwd": "/",
                "root": "/",
                "cmdline": (path,),
                "maps": (),
                "fds": (),
            }),
        ),
        start_time_ticks=start,
    )


def test_public_observer_api_does_not_expose_test_seams() -> None:
    with pytest.raises(TypeError):
        observer.observe_and_validate_release_host(
            phase=inventory.InventoryPhase.PREDECESSOR_ACTIVE,
            predecessor_revision=PREDECESSOR,
            target_revision=TARGET,
            production=False,  # type: ignore[call-arg]
        )


def test_collects_valid_inventory_and_returns_self_hashed_receipt() -> None:
    harness = _harness()

    result = _observe(harness)

    assert result.validation.phase is inventory.InventoryPhase.PREDECESSOR_ACTIVE
    assert len(result.unit_observations) == inventory.EXPECTED_UNIT_COUNT
    assert result.process_observations == ()
    validated = observer.validate_host_observation_receipt(result.receipt)
    assert validated["receipt_sha256"] == result.receipt["receipt_sha256"]
    assert validated["systemd"]["canonical_unit_count"] == inventory.EXPECTED_UNIT_COUNT
    assert validated["processes"]["scanned_process_count"] == 0
    assert (
        validated["validation"]["long_running_service_count"]
        == inventory.EXPECTED_LONG_RUNNING_SERVICE_COUNT
    )
    assert (
        validated["validation"]["startup_oneshot_service_count"]
        == inventory.EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT
    )
    assert (
        validated["validation"]["triggered_oneshot_service_count"]
        == inventory.EXPECTED_TRIGGERED_ONESHOT_SERVICE_COUNT
    )
    assert (
        validated["validation"]["oneshot_service_count"]
        == inventory.EXPECTED_ONESHOT_SERVICE_COUNT
    )
    assert "cmdline" not in validated["processes"]


def test_receipt_hash_rejects_tampering() -> None:
    result = _observe(_harness())
    tampered = dict(result.receipt)
    tampered["observed_at_unix_ns"] += 1

    with pytest.raises(
        observer.ProductionReleaseHostObserverError,
        match="production_release_host_receipt_hash_invalid",
    ):
        observer.validate_host_observation_receipt(tampered)


def test_omitted_unknown_unit_in_show_output_fails_closed() -> None:
    harness = _harness(extra_unit_names=("unknown-release.service",))

    with pytest.raises(
        observer.ProductionReleaseHostObserverError,
        match="production_release_host_systemctl_show_coverage_invalid",
    ):
        _observe(harness)


def test_non_runnable_template_definition_is_accounted_without_show() -> None:
    harness = _harness(extra_unit_names=("example@.service",))

    result = _observe(harness)

    assert result.receipt["systemd"]["enumerated_name_count"] == (
        inventory.EXPECTED_UNIT_COUNT + 1
    )
    assert result.receipt["systemd"]["non_runnable_template_name_count"] == 1


def test_escaped_unrelated_systemd_identity_is_scanned_then_accounted() -> None:
    spec = inventory.ConsumerSpec(
        name=r"escaped@dev\x2dfoo.service",
        source="test",
        kind="service",
        fragment_path="/etc/systemd/system/escaped-device.service",
    )
    harness = _harness(
        extra_blocks={spec.name: _properties(spec)},
        extra_unit_names=(spec.name,),
    )

    result = _observe(harness)

    assert result.receipt["systemd"]["incompatible_unrelated_unit_count"] == 1


def test_complete_unknown_release_unit_is_not_silently_ignored() -> None:
    spec = inventory.ConsumerSpec(
        name="unknown-release.service",
        source="test",
        kind="service",
        fragment_path="/etc/systemd/system/unknown-release.service",
        executes_release=True,
    )
    values = _properties(spec)
    harness = _harness(
        extra_blocks={spec.name: values},
        extra_unit_names=(spec.name,),
    )

    with pytest.raises(
        observer.ProductionReleaseHostObserverError,
        match=(
            "production_release_host_inventory_invalid:"
            "production_release_consumer_unknown"
        ),
    ):
        _observe(harness)


def test_unit_path_swap_between_fenced_snapshots_is_rejected() -> None:
    harness = _harness()
    swap_path = next(iter(harness.catalog.values())).fragment_path
    harness.files.swap_path = swap_path

    with pytest.raises(
        observer.ProductionReleaseHostObserverError,
        match="production_release_host_systemd_snapshot_raced",
    ):
        _observe(harness)


def test_proc_pid_reuse_between_identity_fences_is_rejected() -> None:
    process = _process(
        pid=41,
        start=100,
        unit=None,
        path="/usr/bin/sleep",
    )
    proc = _FakeProc(
        identity_snapshots=[{41: 100}, {41: 101}, {41: 101}],
        processes={41: process},
    )
    harness = _harness(proc=proc)

    with pytest.raises(
        observer.ProductionReleaseHostObserverError,
        match="production_release_host_proc_snapshot_raced",
    ):
        _observe(harness)


def test_unattributed_release_process_is_rejected() -> None:
    process = _process(
        pid=51,
        start=200,
        unit=None,
        path=f"{_release_root(PREDECESSOR)}/venv/bin/python3",
    )
    proc = _FakeProc(
        identity_snapshots=[{51: 200}, {51: 200}, {51: 200}],
        processes={51: process},
    )
    harness = _harness(proc=proc)

    with pytest.raises(
        observer.ProductionReleaseHostObserverError,
        match=(
            "production_release_host_inventory_invalid:"
            "production_release_consumer_process_unknown"
        ),
    ):
        _observe(harness)


def test_mutable_compatibility_symlink_process_reference_is_rejected() -> None:
    execution_unit = next(
        name
        for name, spec in inventory.expected_consumer_catalog().items()
        if spec.executes_release
    )
    process = _process(
        pid=61,
        start=300,
        unit=execution_unit,
        path="/opt/adventico-ai-platform/hermes-agent/run_agent.py",
    )
    proc = _FakeProc(
        identity_snapshots=[{61: 300}, {61: 300}, {61: 300}],
        processes={61: process},
    )
    harness = _harness(proc=proc)

    with pytest.raises(
        observer.ProductionReleaseHostObserverError,
        match=(
            "production_release_host_inventory_invalid:"
            "production_release_consumer_release_symlink_forbidden"
        ),
    ):
        _observe(harness)


def test_incomplete_systemctl_show_property_set_is_rejected() -> None:
    harness = _harness()
    first_name = sorted(harness.blocks)[0]
    incomplete = {name: dict(values) for name, values in harness.blocks.items()}
    incomplete[first_name].pop("NeedDaemonReload")
    raw = "\n\n".join(
        "\n".join(f"{key}={value}" for key, value in values.items())
        for _, values in sorted(incomplete.items())
    ).encode()
    harness.runner.show_outputs = [raw]

    with pytest.raises(
        observer.ProductionReleaseHostObserverError,
        match="production_release_host_systemctl_show_incomplete",
    ):
        _observe(harness)
