from __future__ import annotations

import hashlib
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import pytest

from ops.muncho.runtime import upstream_sync_job_rail as rail
from scripts.canary import package_production_cutover_artifacts as host_package
from scripts.canary import production_release_consumer_inventory as inventory
from scripts.canary import upstream_sync_rail_cutover as activation
from scripts.canary import upstream_sync_rail_successor_rebind as successor


ROOT = Path(__file__).parents[3]
PREDECESSOR = "9d4a56cb069c096a2db6e452c19ffc1b7dc2d4f6"
PREDECESSOR_SENDER = "f8733e2f44dae583ac30b2c4f4e85afd7890a1a5"
TARGET = "a094bc4c2ecf1e4deb9b5b353491f9a0690211b3"


def test_stage_c_contract_inventories_both_exact_timer_service_pairs() -> None:
    catalog = inventory.expected_consumer_catalog()
    expected_paths = {
        rail.SYNC_SERVICE_UNIT: "dual_upstream_sync_service_unit",
        rail.SYNC_TIMER_UNIT: "dual_upstream_sync_timer_unit",
        rail.REPORT_SERVICE_UNIT: "dual_upstream_sync_report_service_unit",
        rail.REPORT_TIMER_UNIT: "dual_upstream_sync_report_timer_unit",
    }
    for unit, artifact_name in expected_paths.items():
        target, binding = host_package.HOST_ARTIFACT_TARGETS[artifact_name]
        assert target == f"/etc/systemd/system/{unit}"
        assert binding == "owner_runtime_rendered"
        assert catalog[unit].fragment_path == target
        assert catalog[unit].source == "host"
    assert catalog[rail.SYNC_SERVICE_UNIT].triggered_by == (
        rail.SYNC_TIMER_UNIT,
    )
    assert catalog[rail.SYNC_TIMER_UNIT].triggers == (
        rail.SYNC_SERVICE_UNIT,
    )
    assert catalog[rail.REPORT_SERVICE_UNIT].triggered_by == (
        rail.REPORT_TIMER_UNIT,
    )
    assert catalog[rail.REPORT_TIMER_UNIT].triggers == (
        rail.REPORT_SERVICE_UNIT,
    )


def _write(path: Path, raw: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


def _write_canonical(path: Path, value: dict[str, Any]) -> None:
    _write(path, activation._canonical(value) + b"\n", mode=0o444)  # noqa: SLF001


class FakeSystemd:
    def __init__(
        self,
        *,
        systemd_root: Path,
        predecessor_digests: dict[str, str],
        target_digests: dict[str, str],
        timer_enabled_state: str = "enabled",
        timer_active_state: str = "active",
    ) -> None:
        self.systemd_root = systemd_root
        self.predecessor_digests = predecessor_digests
        self.target_digests = target_digests
        self.calls: list[tuple[str, ...]] = []
        self.fail_catch_up = False
        self.fail_first_timer_stop = False
        self.states = {
            name: successor.UnitState(
                unit=name,
                loaded=True,
                fragment_path=str(systemd_root / name),
                fragment_sha256=predecessor_digests[name],
                enabled_state=(
                    timer_enabled_state
                    if name in successor.TIMER_NAMES
                    else "static"
                ),
                active_state=(
                    timer_active_state
                    if name in successor.TIMER_NAMES
                    else "inactive"
                ),
                assert_result="no",
                result=("success" if name in successor.SERVICE_NAMES else ""),
                exec_main_status=(0 if name in successor.SERVICE_NAMES else None),
            )
            for name in successor.UNIT_NAMES
        }

    def observe(
        self,
        unit: str,
        *,
        systemd_root: Path,
    ) -> successor.UnitState:
        assert systemd_root == self.systemd_root
        return self.states[unit]

    def _set(self, name: str, **changes: Any) -> None:
        self.states[name] = replace(self.states[name], **changes)

    def _reload(self) -> None:
        for name in successor.UNIT_NAMES:
            digest = hashlib.sha256(
                (self.systemd_root / name).read_bytes()
            ).hexdigest()
            self._set(
                name,
                fragment_sha256=digest,
                assert_result=("yes" if digest == self.target_digests[name] else "no"),
                active_state="inactive",
            )

    def mutate(self, *arguments: str) -> None:
        self.calls.append(tuple(arguments))
        action, *units = arguments
        if action == "daemon-reload" and not units:
            self._reload()
            return
        assert set(units).issubset(set(successor.UNIT_NAMES))
        if action == "stop":
            for name in units:
                self._set(name, active_state="inactive")
            if self.fail_first_timer_stop:
                self.fail_first_timer_stop = False
                self._set(successor.TIMER_NAMES[0], active_state="active")
            return
        if action == "enable":
            for name in units:
                self._set(name, enabled_state="enabled")
            return
        if action == "disable":
            for name in units:
                self._set(name, enabled_state="disabled")
            return
        if action == "start":
            for name in units:
                if name in successor.TIMER_NAMES:
                    self._set(name, active_state="active")
                else:
                    failed = self.fail_catch_up and name == rail.SYNC_SERVICE_UNIT
                    self._set(
                        name,
                        active_state="inactive",
                        result=("failed" if failed else "success"),
                        exec_main_status=(1 if failed else 0),
                    )
            return
        raise AssertionError(arguments)


@dataclass
class Harness:
    staged_root: Path
    authority_path: Path
    preflight_path: Path
    runtime_path: Path
    systemd_root: Path
    evidence_root: Path
    releases: Path
    package: activation.PackageContext
    predecessor_units: dict[str, bytes]
    authority: dict[str, Any]
    preflight: dict[str, Any]
    host: FakeSystemd

    def rebind(
        self,
        *,
        progress_hook: Callable[[str, str | None], None] | None = None,
    ) -> dict[str, Any]:
        return successor._rebind(  # noqa: SLF001
            expected_authority_sha256=self.authority["authority_sha256"],
            expected_preflight_sha256=self.preflight["receipt_sha256"],
            staged_root=self.staged_root,
            authority_path=self.authority_path,
            preflight_path=self.preflight_path,
            runtime_path=self.runtime_path,
            systemd_root=self.systemd_root,
            evidence_root=self.evidence_root,
            root_owned=False,
            release_trust_root=self.releases,
            host=self.host,
            require_root=False,
            activation_lock_factory=lambda: nullcontext(),
            progress_hook=progress_hook,
        )


def _build_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    timer_enabled_state: str = "enabled",
    timer_active_state: str = "active",
) -> Harness:
    releases = tmp_path / "releases"
    monkeypatch.setattr(rail, "RELEASES_ROOT", releases)
    target_release = rail.release_root(TARGET)
    for relative in (
        rail.RAIL_RELATIVE,
        rail.MUNCHO_ROUTINE_RELATIVE,
        rail.HARDENING_RELATIVE,
        rail.SKYAI_ROUTINE_RELATIVE,
        rail.REPORTER_RELATIVE,
    ):
        target = target_release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    _write(
        target_release / rail.SOURCE_MARKER_RELATIVE,
        rail.exact_revision_marker(TARGET),
        mode=0o444,
    )
    _write(
        target_release / ".venv/bin/python",
        b"reviewed-target-python\n",
        mode=0o755,
    )
    monkeypatch.setattr(rail, "validate_credential_metadata", lambda: None)
    monkeypatch.setattr(
        rail,
        "host_binary_fact",
        lambda path: "1" * 64 if path == rail.GH_PATH else "2" * 64,
    )
    built = rail.build_package(TARGET, TARGET)
    staged_root = tmp_path / "staged"
    rail.stage_package(built, output_root=staged_root)
    package = activation._validate_package_context(  # noqa: SLF001
        staged_root=staged_root,
        release_revision=TARGET,
        sender_revision=TARGET,
        expected_manifest_sha256=built.manifest_sha256,
        root_owned=False,
        staged_trust_root=staged_root,
        release_trust_root=releases,
    )

    predecessor_units = {
        rail.SYNC_SERVICE_UNIT: rail.render_sync_service(
            revision=PREDECESSOR,
            release=rail.release_root(PREDECESSOR),
            source_digests=built.source_digests,
            binary_digests=built.host_binary_digests,
        ),
        rail.SYNC_TIMER_UNIT: rail.render_sync_timer(),
        rail.REPORT_SERVICE_UNIT: rail.render_report_service(
            release=rail.release_root(PREDECESSOR),
            sender_release=rail.release_root(PREDECESSOR_SENDER),
            sender_python_sha256="3" * 64,
        ),
        rail.REPORT_TIMER_UNIT: rail.render_report_timer(),
    }
    systemd_root = tmp_path / "systemd"
    for name, raw in predecessor_units.items():
        _write(systemd_root / name, raw, mode=0o644)

    runtime_path = ROOT / successor.RUNTIME_RELATIVE
    authority = successor.build_authority(
        package=package,
        predecessor_revision=PREDECESSOR,
        predecessor_sender_revision=PREDECESSOR_SENDER,
        predecessor_units=predecessor_units,
        stage_c_host_artifact_manifest_sha256="4" * 64,
        stage_c_release_update_publication_sha256="5" * 64,
        rebind_runtime_sha256=hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
    )
    authority_path = staged_root / "successor-rebind-authority.json"
    _write_canonical(authority_path, authority)
    host = FakeSystemd(
        systemd_root=systemd_root,
        predecessor_digests=dict(authority["predecessor_unit_digests"]),
        target_digests=dict(authority["target_unit_digests"]),
        timer_enabled_state=timer_enabled_state,
        timer_active_state=timer_active_state,
    )
    preflight = successor.preflight(
        expected_authority_sha256=authority["authority_sha256"],
        staged_root=staged_root,
        authority_path=authority_path,
        runtime_path=runtime_path,
        systemd_root=systemd_root,
        root_owned=False,
        release_trust_root=releases,
        host=host,
    )
    preflight_path = staged_root / "successor-rebind-preflight.json"
    _write_canonical(preflight_path, preflight)
    return Harness(
        staged_root=staged_root,
        authority_path=authority_path,
        preflight_path=preflight_path,
        runtime_path=runtime_path,
        systemd_root=systemd_root,
        evidence_root=tmp_path / "evidence",
        releases=releases,
        package=package,
        predecessor_units=predecessor_units,
        authority=authority,
        preflight=preflight,
        host=host,
    )


def test_exact_9d4_f873_successor_rebind_proves_target_and_catch_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)

    terminal = harness.rebind()

    assert terminal["target_revision"] == TARGET
    assert terminal["forward_recovery_performed"] is False
    assert terminal["assert_result"] == {
        name: "yes" for name in successor.UNIT_NAMES
    }
    assert harness.host.calls.count(("stop", *successor.TIMER_NAMES)) == 1
    assert ("start", *successor.SERVICE_NAMES) in harness.host.calls
    assert ("start", *successor.TIMER_NAMES) in harness.host.calls
    for name in successor.UNIT_NAMES:
        assert (harness.systemd_root / name).read_bytes() == harness.package.artifacts[name]


def test_foreign_predecessor_tamper_fails_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    _write(
        harness.systemd_root / rail.SYNC_SERVICE_UNIT,
        b"foreign-unit\n",
        mode=0o644,
    )

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="predecessor_unit_drifted",
    ):
        successor.preflight(
            expected_authority_sha256=harness.authority["authority_sha256"],
            staged_root=harness.staged_root,
            authority_path=harness.authority_path,
            runtime_path=harness.runtime_path,
            systemd_root=harness.systemd_root,
            root_owned=False,
            release_trust_root=harness.releases,
            host=harness.host,
        )
    assert harness.host.calls == []


@pytest.mark.parametrize(
    "changes",
    (
        {"enabled_state": "disabled"},
        {"active_state": "inactive"},
    ),
)
def test_stale_preflight_timer_state_fails_before_started_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, str],
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    harness.host._set(rail.SYNC_TIMER_UNIT, **changes)

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="timer_prestate_drifted",
    ):
        harness.rebind()

    assert harness.host.calls == []
    assert not successor._transaction_path(  # noqa: SLF001
        harness.authority["authority_sha256"],
        "started.json",
        evidence_root=harness.evidence_root,
    ).exists()


def test_failed_catch_up_restores_exact_predecessor_and_timer_prestates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    harness.host.fail_catch_up = True

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="failed_rolled_back:upstream_sync_successor_catch_up_unconfirmed",
    ):
        harness.rebind()

    for name, raw in harness.predecessor_units.items():
        assert (harness.systemd_root / name).read_bytes() == raw
    for name in successor.TIMER_NAMES:
        assert harness.host.states[name].enabled_state == "enabled"
        assert harness.host.states[name].active_state == "active"
    rollback = activation._read_canonical_json(  # noqa: SLF001
        successor._transaction_path(  # noqa: SLF001
            harness.authority["authority_sha256"],
            "rollback.json",
            evidence_root=harness.evidence_root,
        ),
        root_owned=False,
        modes=frozenset({0o600}),
    )
    assert rollback["archive_used"] is True
    assert rollback["rollback_complete"] is True


def test_failure_before_archive_restores_timer_prestates_without_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    harness.host.fail_first_timer_stop = True

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="failed_rolled_back:upstream_sync_successor_quiescence_unconfirmed",
    ):
        harness.rebind()

    for name, raw in harness.predecessor_units.items():
        assert (harness.systemd_root / name).read_bytes() == raw
    rollback = activation._read_canonical_json(  # noqa: SLF001
        successor._transaction_path(  # noqa: SLF001
            harness.authority["authority_sha256"],
            "rollback.json",
            evidence_root=harness.evidence_root,
        ),
        root_owned=False,
        modes=frozenset({0o600}),
    )
    assert rollback["archive_used"] is False
    assert rollback["rollback_complete"] is True


def test_rollback_restores_exact_disabled_inactive_timer_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(
        tmp_path,
        monkeypatch,
        timer_enabled_state="disabled",
        timer_active_state="inactive",
    )
    harness.host.fail_catch_up = True

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="failed_rolled_back:upstream_sync_successor_catch_up_unconfirmed",
    ):
        harness.rebind()

    for name in successor.TIMER_NAMES:
        assert harness.host.states[name].enabled_state == "disabled"
        assert harness.host.states[name].active_state == "inactive"


def test_crash_mid_rollback_resumes_rollback_before_forward_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    harness.host.fail_catch_up = True
    rollback_writes = 0

    def crash_during_rollback(event: str, _unit: str | None) -> None:
        nonlocal rollback_writes
        if event != "rollback_unit_restored":
            return
        rollback_writes += 1
        if rollback_writes == 1:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        harness.rebind(progress_hook=crash_during_rollback)

    intent_path = successor._transaction_path(  # noqa: SLF001
        harness.authority["authority_sha256"],
        "rollback-intent.json",
        evidence_root=harness.evidence_root,
    )
    rollback_path = successor._transaction_path(  # noqa: SLF001
        harness.authority["authority_sha256"],
        "rollback.json",
        evidence_root=harness.evidence_root,
    )
    assert intent_path.exists()
    assert not rollback_path.exists()
    intent = activation._read_canonical_json(  # noqa: SLF001
        intent_path,
        root_owned=False,
        modes=frozenset({0o600}),
    )
    assert intent["rollback_must_resume_before_forward_recovery"] is True
    assert intent["forward_mutation_may_have_occurred"] is True
    assert intent["rollback_mutation_performed"] is False

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="failed_rolled_back:upstream_sync_successor_catch_up_unconfirmed",
    ):
        harness.rebind()

    assert rollback_path.exists()
    rollback = activation._read_canonical_json(  # noqa: SLF001
        rollback_path,
        root_owned=False,
        modes=frozenset({0o600}),
    )
    assert rollback["rollback_intent_receipt_sha256"] == intent["receipt_sha256"]
    for name, raw in harness.predecessor_units.items():
        assert (harness.systemd_root / name).read_bytes() == raw
    assert harness.host.calls.count(("start", *successor.SERVICE_NAMES)) == 1


def test_crash_mid_replace_recovers_forward_from_exact_digest_mix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    replacements = 0

    def crash_after_second(event: str, _unit: str | None) -> None:
        nonlocal replacements
        assert event == "unit_replaced"
        replacements += 1
        if replacements == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        harness.rebind(progress_hook=crash_after_second)

    terminal = harness.rebind()

    assert terminal["forward_recovery_performed"] is True
    for name in successor.UNIT_NAMES:
        assert (harness.systemd_root / name).read_bytes() == harness.package.artifacts[name]


def test_foreign_tamper_after_crash_rolls_back_instead_of_recovering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)

    def crash(event: str, unit: str | None) -> None:
        assert event == "unit_replaced"
        assert unit is not None
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        harness.rebind(progress_hook=crash)
    _write(
        harness.systemd_root / rail.SYNC_SERVICE_UNIT,
        b"foreign-after-start\n",
        mode=0o644,
    )

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="failed_rolled_back:upstream_sync_successor_foreign_unit_drift",
    ):
        harness.rebind()
    for name, raw in harness.predecessor_units.items():
        assert (harness.systemd_root / name).read_bytes() == raw


def test_systemd_observer_normalizes_known_omitted_optional_properties(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    systemd_root = tmp_path / "systemd"
    unit = rail.SYNC_SERVICE_UNIT
    _write(systemd_root / unit, b"[Service]\nType=oneshot\n", mode=0o644)
    required = {
        "LoadState": b"loaded\n",
        "FragmentPath": f"{systemd_root / unit}\n".encode(),
        "ActiveState": b"inactive\n",
    }
    monkeypatch.setattr(
        activation,
        "_systemctl_property",
        lambda _unit, name: required.get(name, b"\n"),
    )
    monkeypatch.setattr(
        activation,
        "_systemctl_capture",
        lambda *args: (1, b"disabled\n")
        if args == ("is-enabled", unit)
        else (_ for _ in ()).throw(AssertionError(args)),
    )

    observed = successor._SystemdHost().observe(  # noqa: SLF001
        unit,
        systemd_root=systemd_root,
    )

    assert observed.assert_result == ""
    assert observed.result == ""
    assert observed.exec_main_status is None


def test_systemd_observer_rejects_duplicate_property_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    systemd_root = tmp_path / "systemd"
    unit = rail.SYNC_SERVICE_UNIT
    _write(systemd_root / unit, b"[Service]\nType=oneshot\n", mode=0o644)
    required = {
        "LoadState": b"loaded\n",
        "FragmentPath": f"{systemd_root / unit}\n".encode(),
        "ActiveState": b"inactive\n",
        "ExecMainStatus": b"0\n",
        "AssertResult": b"yes\nyes\n",
    }
    monkeypatch.setattr(
        activation,
        "_systemctl_property",
        lambda _unit, name: required.get(name, b"\n"),
    )
    monkeypatch.setattr(
        activation,
        "_systemctl_capture",
        lambda *args: (0, b"static\n")
        if args == ("is-enabled", unit)
        else (_ for _ in ()).throw(AssertionError(args)),
    )

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="systemd_observation_invalid",
    ):
        successor._SystemdHost().observe(  # noqa: SLF001
            unit,
            systemd_root=systemd_root,
        )
