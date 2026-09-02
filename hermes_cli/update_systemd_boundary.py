"""Fail-closed systemd worker-generation boundary for ``hermes update``.

The generic update inventory remains cross-platform.  This module is the
Linux/systemd-specific execution boundary: it snapshots exact user units,
quiesces them, starts a canary generation, and reconciles the complete unit
set before health/e2e monitors are re-armed.  All command execution and
runtime identity collection are injected so tests never contact a real user
manager.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


class WorkerBoundaryError(RuntimeError):
    """The generation boundary could not prove a complete, fresh fleet."""


@dataclass(frozen=True)
class SystemdUnit:
    name: str
    load_state: str
    enabled_state: str
    active_state: str
    sub_state: str
    main_pid: int
    start_monotonic_usec: int
    exec_start: str
    fragment_path: str

    @property
    def enabled(self) -> bool:
        return self.enabled_state in {
            "enabled",
            "enabled-runtime",
            "linked",
            "linked-runtime",
        }

    @property
    def running(self) -> bool:
        return self.main_pid > 0 or self.active_state in {
            "active",
            "activating",
            "reloading",
        }


@dataclass(frozen=True)
class WorkerBoundarySnapshot:
    targets: Mapping[str, SystemdUnit]
    monitors: tuple[str, ...]
    concierge_e2e_services: tuple[str, ...]


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]
IdentityCollector = Callable[[], Mapping[str, Mapping[str, str]]]
CanaryVerifier = Callable[[SystemdUnit], bool]

_SHOW_PROPERTIES = (
    "Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,"
    "ExecMainStartTimestampMonotonic,ExecStart,FragmentPath"
)
_ENABLED_STATES = {"enabled", "enabled-runtime", "linked", "linked-runtime"}


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )


def _unit_is_target(name: str) -> bool:
    return (
        (name.startswith("hermes-gateway") and name.endswith(".service"))
        or name in {"hermes-dashboard.service", "hermes-webui.service"}
    )


def _unit_is_monitor(name: str) -> bool:
    lowered = name.lower()
    return (
        name.endswith(".timer")
        and "buzz" in lowered
        and ("health" in lowered or "e2e" in lowered)
    )


def _unit_is_concierge_e2e(name: str) -> bool:
    lowered = name.lower()
    return (
        name.endswith(".service")
        and "buzz" in lowered
        and "concierge" in lowered
        and "e2e" in lowered
    )


def _parse_list_names(text: str) -> set[str]:
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        name = line.split(None, 1)[0]
        if name.endswith((".service", ".timer")):
            names.add(name)
    return names


def _parse_show(text: str) -> dict[str, SystemdUnit]:
    records: dict[str, SystemdUnit] = {}
    for block in text.strip().split("\n\n") if text.strip() else []:
        values: dict[str, str] = {}
        for line in block.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        name = values.get("Id", "")
        if not name:
            continue
        try:
            pid = int(values.get("MainPID", "0") or 0)
            started = int(values.get("ExecMainStartTimestampMonotonic", "0") or 0)
        except ValueError as exc:
            raise WorkerBoundaryError(
                f"systemd returned invalid process metadata for {name}: {exc}"
            ) from exc
        records[name] = SystemdUnit(
            name=name,
            load_state=values.get("LoadState", ""),
            enabled_state=values.get("UnitFileState", ""),
            active_state=values.get("ActiveState", ""),
            sub_state=values.get("SubState", ""),
            main_pid=pid,
            start_monotonic_usec=started,
            exec_start=values.get("ExecStart", ""),
            fragment_path=values.get("FragmentPath", ""),
        )
    return records


class SystemdWorkerBoundary:
    """Own one update's exact user-service generation transition."""

    def __init__(
        self,
        runner: Runner = _default_runner,
        *,
        identity_collector: IdentityCollector | None = None,
        canary_verifier: CanaryVerifier | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._identity_collector = identity_collector
        self._canary_verifier = canary_verifier
        self._monotonic = monotonic

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        command = ["systemctl", "--user", *args]
        try:
            result = self._runner(command)
        except Exception as exc:
            raise WorkerBoundaryError(
                f"systemd user-manager command failed: {' '.join(command)}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no diagnostic").strip()
            raise WorkerBoundaryError(
                f"systemd user-manager command failed: {' '.join(command)}: {detail}"
            )
        return result

    def _show(self, names: Sequence[str]) -> dict[str, SystemdUnit]:
        if not names:
            return {}
        result = self._run(
            "show",
            *sorted(set(names)),
            f"--property={_SHOW_PROPERTIES}",
            "--no-pager",
        )
        units = _parse_show(result.stdout or "")
        missing = sorted(set(names) - set(units))
        if missing:
            raise WorkerBoundaryError(
                "systemd omitted requested unit metadata: " + ", ".join(missing)
            )
        return units

    def inventory(self) -> WorkerBoundarySnapshot:
        """Enumerate exact installed/loaded units without shell patterns."""
        files = self._run(
            "list-unit-files",
            "--type=service,timer",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
        )
        loaded = self._run(
            "list-units",
            "--type=service,timer",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
        )
        names = _parse_list_names(files.stdout or "") | _parse_list_names(
            loaded.stdout or ""
        )
        relevant = sorted(
            name
            for name in names
            if _unit_is_target(name)
            or _unit_is_monitor(name)
            or _unit_is_concierge_e2e(name)
        )
        units = self._show(relevant)
        targets = {
            name: unit
            for name, unit in units.items()
            if _unit_is_target(name) and (unit.enabled or unit.running)
        }
        monitors = tuple(
            sorted(
                name
                for name, unit in units.items()
                if _unit_is_monitor(name) and (unit.enabled or unit.running)
            )
        )
        concierge = tuple(
            sorted(
                name
                for name, unit in units.items()
                # One-shot services are commonly static/inactive. Presence as
                # a loaded unit is enough to make them an installed lifecycle
                # hook; unlike worker targets, they need not be enabled.
                if _unit_is_concierge_e2e(name) and unit.load_state == "loaded"
            )
        )
        return WorkerBoundarySnapshot(targets, monitors, concierge)

    def _recover_hint(self, units: Sequence[str]) -> str:
        names = ", ".join(sorted(set(units))) or "the affected Hermes units"
        return (
            f"Affected units: {names}. Monitors were left paused. Recover with "
            "`systemctl --user reset-failed <unit>` and "
            "`systemctl --user restart <unit>`, then verify the fleet before "
            "starting its Buzz health/e2e timers."
        )

    def _verify_wave(
        self,
        names: Sequence[str],
        before: WorkerBoundarySnapshot,
        generation_usec: int,
        expected_identity: Mapping[str, str] | None,
    ) -> dict[str, SystemdUnit]:
        current = self._show(names)
        failures: list[str] = []
        identities = self._identity_collector() if self._identity_collector else {}
        for name in names:
            old = before.targets[name]
            new = current[name]
            if new.active_state != "active" or new.main_pid <= 0:
                failures.append(f"{name} is {new.active_state}/{new.sub_state} pid={new.main_pid}")
                continue
            if new.main_pid == old.main_pid:
                failures.append(f"{name} retained stale MainPID {new.main_pid}")
            if new.start_monotonic_usec <= max(
                generation_usec, old.start_monotonic_usec
            ):
                failures.append(
                    f"{name} has stale start time {new.start_monotonic_usec}"
                )
            if new.exec_start != old.exec_start:
                failures.append(
                    f"{name} ExecStart changed across the update boundary"
                )
            if new.fragment_path != old.fragment_path:
                failures.append(
                    f"{name} unit fragment changed across the update boundary"
                )
            if expected_identity:
                actual = identities.get(name, {})
                for key, expected in expected_identity.items():
                    if expected and actual.get(key) and actual.get(key) != expected:
                        failures.append(
                            f"{name} {key}={actual.get(key)!r}, expected {expected!r}"
                        )
            if self._canary_verifier and not self._canary_verifier(new):
                failures.append(f"{name} failed the canary health check")
        if failures:
            raise WorkerBoundaryError(
                "; ".join(failures) + ". " + self._recover_hint(names)
            )
        return current

    def transition(
        self,
        before: WorkerBoundarySnapshot,
        *,
        expected_identity: Mapping[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Move every inventoried target to one provably fresh generation.

        Ordering is deliberate: pause monitors; stop/quiesce all workers;
        reset the intentional ``failed/MainPID=0`` state; start dashboard,
        WebUI, and one gateway canary; verify; start remaining gateways;
        reconcile exact membership. Monitor re-arm is deliberately a separate
        call: the updater must first complete its authoritative code-SHA fleet
        matrix and plan-vs-execution reconciliation.
        """
        targets = tuple(sorted(before.targets))
        if not targets:
            return ()
        touched = list(targets)
        try:
            for timer in before.monitors:
                self._run("stop", timer)

            generation_usec = int(self._monotonic() * 1_000_000)
            for name in targets:
                self._run("stop", name)

            quiesced = self._show(targets)
            not_quiesced: list[str] = []
            for name, unit in quiesced.items():
                if unit.active_state == "failed" and unit.main_pid == 0:
                    self._run("reset-failed", name)
                elif unit.active_state != "inactive" or unit.main_pid > 0:
                    not_quiesced.append(name)
            if not_quiesced:
                raise WorkerBoundaryError(
                    "workers did not quiesce: " + ", ".join(not_quiesced)
                )

            frontends = [
                name
                for name in ("hermes-dashboard.service", "hermes-webui.service")
                if name in before.targets
            ]
            gateways = sorted(
                (name for name in targets if name.startswith("hermes-gateway")),
                key=lambda name: (name != "hermes-gateway.service", name),
            )
            canary = gateways[:1]
            first_wave = frontends + canary
            for name in first_wave:
                self._run("start", name)
            self._verify_wave(
                first_wave, before, generation_usec, expected_identity
            )

            remaining = [name for name in targets if name not in first_wave]
            for name in remaining:
                self._run("start", name)
            self._verify_wave(
                remaining, before, generation_usec, expected_identity
            )

            after = self.inventory()
            expected = set(before.targets)
            actual = set(after.targets)
            missing = sorted(expected - actual)
            extra_enabled = sorted(
                name
                for name in actual - expected
                if after.targets[name].enabled_state in _ENABLED_STATES
            )
            if missing or extra_enabled:
                parts = []
                if missing:
                    parts.append("missing enabled/running units: " + ", ".join(missing))
                if extra_enabled:
                    parts.append("extra enabled units: " + ", ".join(extra_enabled))
                raise WorkerBoundaryError("; ".join(parts))

            # Exact final state proof, after membership reconciliation.
            self._verify_wave(targets, before, generation_usec, expected_identity)
            return targets
        except WorkerBoundaryError as exc:
            message = str(exc)
            if "Affected units:" not in message:
                message += ". " + self._recover_hint(touched)
            raise WorkerBoundaryError(message) from exc

    def rearm_monitors(self, before: WorkerBoundarySnapshot) -> None:
        """Re-arm optional monitors after the caller proves the whole fleet.

        This method intentionally does no worker verification of its own.  The
        only valid caller is the update tail after both the code-generation
        matrix and runtime-outcome reconciliation have succeeded.  A timer or
        e2e start failure is fatal so an update cannot report success without
        its monitoring contract restored.
        """
        try:
            for timer in before.monitors:
                self._run("enable", "--now", timer)
            for service in before.concierge_e2e_services:
                self._run("start", service)
        except WorkerBoundaryError as exc:
            message = str(exc)
            if "Affected units:" not in message:
                message += ". " + self._recover_hint(tuple(before.targets))
            raise WorkerBoundaryError(message) from exc


def capture_systemd_worker_boundary(
    *,
    relevant: bool,
    runner: Runner = _default_runner,
    identity_collector: IdentityCollector | None = None,
    canary_verifier: CanaryVerifier | None = None,
) -> tuple[SystemdWorkerBoundary, WorkerBoundarySnapshot] | None:
    """Capture the boundary only on a relevant Linux user-systemd install.

    ``relevant`` is supplied by the existing platform capability probe.  Once
    it says systemd applies, inventory errors propagate and abort the update
    before source mutation; absence is never silently interpreted as an empty
    fleet.
    """
    if not relevant or not sys.platform.startswith("linux"):
        return None
    if runner is _default_runner and shutil.which("systemctl") is None:
        raise WorkerBoundaryError(
            "systemd is relevant but systemctl is unavailable; refusing source mutation"
        )
    boundary = SystemdWorkerBoundary(
        runner,
        identity_collector=identity_collector,
        canary_verifier=canary_verifier,
    )
    return boundary, boundary.inventory()
