from __future__ import annotations

import hashlib
import shutil
import stat
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import pytest

from gateway import production_owner_runtime
from ops.muncho.runtime import upstream_sync_job_rail as rail
from scripts.canary import package_production_cutover_artifacts as host_package
from scripts.canary import production_release_update_contract as release_contract
from scripts.canary import production_release_consumer_inventory as inventory
from scripts.canary import production_release_rotation_stager_installer as foundation
from scripts.canary import (
    production_successor_rebind_owner_runtime as foundation_runtime,
)
from scripts.canary import production_cutover_owner_launcher as owner_launcher
from scripts.canary import upstream_sync_rail_cutover as activation
from scripts.canary import upstream_sync_rail_successor_rebind as successor


ROOT = Path(__file__).parents[3]
PREDECESSOR = "9d4a56cb069c096a2db6e452c19ffc1b7dc2d4f6"
PREDECESSOR_SENDER = "f8733e2f44dae583ac30b2c4f4e85afd7890a1a5"
TARGET = "a094bc4c2ecf1e4deb9b5b353491f9a0690211b3"
PREDECESSOR_RECEIPT = "6" * 64
SOURCE_TREE = "8" * 40
STAGE_C_BUILDER_RECEIPT = "9" * 64


def _owner_runtime_kwargs() -> dict[str, str]:
    controller_manifest = foundation.successor_runtime_controller_manifest_from_bytes(
        release_revision=TARGET,
        assets={
            relative: (ROOT / relative).read_bytes()
            for relative in foundation._SUCCESSOR_RUNTIME_CONTROLLER_ASSETS  # noqa: SLF001
        },
    )
    return {
        "source_tree_oid": SOURCE_TREE,
        "stage_c_builder_terminal_receipt_sha256": STAGE_C_BUILDER_RECEIPT,
        "foundation_wrapper_sha256": successor.FOUNDATION_V4_WRAPPER_SHA256,
        "successor_runtime_foundation_wrapper_sha256": (
            successor.SUCCESSOR_RUNTIME_FOUNDATION_WRAPPER_SHA256
        ),
        "successor_runtime_foundation_launcher_sha256": (
            successor.SUCCESSOR_RUNTIME_FOUNDATION_LAUNCHER_SHA256
        ),
        "successor_runtime_controller_manifest_file_sha256": hashlib.sha256(
            activation._canonical(controller_manifest) + b"\n"  # noqa: SLF001
        ).hexdigest(),
        "controller_owner_runtime_manifest_sha256": "a" * 64,
        "controller_owner_runtime_attestation_sha256": "b" * 64,
        "controller_owner_runtime_tree_sha256": "c" * 64,
        "controller_owner_runtime_interpreter_sha256": "d" * 64,
        "remote_owner_runtime_publication_sha256": "e" * 64,
        "remote_owner_runtime_manifest_sha256": "f" * 64,
        "remote_owner_runtime_attestation_sha256": "0" * 64,
        "remote_owner_runtime_tree_sha256": "1" * 64,
        "remote_owner_runtime_interpreter_sha256": "2" * 64,
        "remote_owner_runtime_staging_publication_sha256": "3" * 64,
        "remote_owner_runtime_staging_manifest_sha256": "4" * 64,
        "remote_owner_runtime_staging_attestation_sha256": "5" * 64,
        "remote_owner_runtime_staging_tree_sha256": "6" * 64,
        "remote_owner_runtime_staging_interpreter_sha256": "7" * 64,
        "remote_owner_runtime_staging_pyvenv_cfg_sha256": "8" * 64,
        "remote_owner_runtime_builder_receipt_sha256": "9" * 64,
        "remote_owner_runtime_wheel_sha256": "a" * 64,
        "preexec_verifier_sha256": successor.PREEXEC_VERIFIER_SHA256,
    }


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
    assert catalog[rail.SYNC_SERVICE_UNIT].triggered_by == (rail.SYNC_TIMER_UNIT,)
    assert catalog[rail.SYNC_TIMER_UNIT].triggers == (rail.SYNC_SERVICE_UNIT,)
    assert catalog[rail.REPORT_SERVICE_UNIT].triggered_by == (rail.REPORT_TIMER_UNIT,)
    assert catalog[rail.REPORT_TIMER_UNIT].triggers == (rail.REPORT_SERVICE_UNIT,)


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
                    timer_enabled_state if name in successor.TIMER_NAMES else "static"
                ),
                active_state=(
                    timer_active_state if name in successor.TIMER_NAMES else "inactive"
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
            digest = hashlib.sha256((self.systemd_root / name).read_bytes()).hexdigest()
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
class FakeStage0Bundle:
    release_root: Path
    host_manifest_sha256: str
    publication_sha256: str
    predecessor_trust_revision: str = PREDECESSOR
    publication_predecessor_revision: str = PREDECESSOR
    plan_predecessor_revision: str = PREDECESSOR
    stable_hook: Callable[[], None] | None = None
    stable_assertions: int = 0

    @property
    def predecessor_trust(self) -> dict[str, Any]:
        return {
            "release_revision": self.predecessor_trust_revision,
            "activation_receipt_sha256": PREDECESSOR_RECEIPT,
        }

    @property
    def publication(self) -> dict[str, Any]:
        return {
            "predecessor_revision": self.publication_predecessor_revision,
            "release_revision": TARGET,
            "publication_sha256": self.publication_sha256,
            "plan": {
                "predecessor_revision": self.plan_predecessor_revision,
                "release_revision": TARGET,
                "host_artifact_manifest_sha256": self.host_manifest_sha256,
                "predecessor_activation_receipt_sha256": PREDECESSOR_RECEIPT,
                "source_tree_oid": SOURCE_TREE,
                "builder_terminal_receipt_sha256": STAGE_C_BUILDER_RECEIPT,
            },
        }

    @property
    def input_documents(self) -> dict[str, dict[str, Any]]:
        return {
            "host_artifact_manifest_sha256": {
                "manifest_sha256": self.host_manifest_sha256,
            }
        }

    @property
    def input_internal_identities(self) -> dict[str, str]:
        return {"host_artifact_manifest_sha256": self.host_manifest_sha256}

    @property
    def builder_manifest(self) -> dict[str, str]:
        return {"release_revision": TARGET}

    @property
    def builder_receipt(self) -> dict[str, str]:
        return {
            "release_revision": TARGET,
            "source_tree_oid": SOURCE_TREE,
            "receipt_sha256": STAGE_C_BUILDER_RECEIPT,
        }

    def assert_stable(self) -> None:
        self.stable_assertions += 1
        if self.stable_hook is not None:
            self.stable_hook()

    def __enter__(self) -> FakeStage0Bundle:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


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


@dataclass
class OwnerHarness:
    harness: Harness
    request: dict[str, Any]
    host_manifest_path: Path
    publication_path: Path
    stage0_bundle: FakeStage0Bundle

    def apply(
        self,
        *,
        stage0_verifier: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        item = self.harness

        def valid_stage0(
            *,
            expected_predecessor_activation_receipt_sha256: str,
        ) -> FakeStage0Bundle:
            assert expected_predecessor_activation_receipt_sha256 == PREDECESSOR_RECEIPT
            return self.stage0_bundle

        selected_verifier = stage0_verifier or valid_stage0

        return successor._owner_apply(  # noqa: SLF001
            self.request,
            staged_root=item.staged_root,
            authority_path=item.authority_path,
            preflight_path=item.preflight_path,
            runtime_path=item.runtime_path,
            systemd_root=item.systemd_root,
            evidence_root=item.evidence_root,
            release_trust_root=item.releases,
            root_owned=False,
            host=item.host,
            require_root=False,
            activation_lock_factory=lambda: nullcontext(),
            stage0_verifier=selected_verifier,
        )


def _self_hashed(
    unsigned: dict[str, Any],
    digest_field: str,
) -> dict[str, Any]:
    return {
        **unsigned,
        digest_field: hashlib.sha256(
            activation._canonical(unsigned)  # noqa: SLF001
        ).hexdigest(),
    }


def _build_owner_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> OwnerHarness:
    harness = _build_harness(tmp_path, monkeypatch)
    release_runtime = rail.release_root(TARGET) / successor.RUNTIME_RELATIVE
    release_runtime.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / successor.RUNTIME_RELATIVE, release_runtime)
    for relative in (
        *foundation._SUCCESSOR_RUNTIME_CONTROLLER_ASSETS,  # noqa: SLF001
        "ops/muncho/release-updater/muncho-successor-runtime-foundation-exec",
    ):
        target = release_runtime.parents[2] / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    harness.runtime_path = release_runtime
    harness.authority_path.unlink()
    harness.preflight_path.unlink()
    host_manifest = _self_hashed(
        {
            "schema": host_package.MANIFEST_SCHEMA,
            "release_revision": TARGET,
            "secret_material_recorded": False,
        },
        "manifest_sha256",
    )
    publication = _self_hashed(
        {
            "schema": release_contract.PUBLICATION_SCHEMA,
            "action": release_contract.PUBLICATION_ACTION,
            "release_revision": TARGET,
            "plan": {
                "release_revision": TARGET,
                "host_artifact_manifest_sha256": host_manifest["manifest_sha256"],
            },
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        },
        "publication_sha256",
    )
    stage_c = tmp_path / "stage-c"
    host_manifest_path = stage_c / "host-artifact-manifest.json"
    publication_path = stage_c / "release-update-publication.json"
    _write_canonical(host_manifest_path, host_manifest)
    _write_canonical(publication_path, publication)
    request = successor.build_owner_request(
        target_revision=TARGET,
        target_package_manifest_sha256=harness.package.manifest["manifest_sha256"],
        predecessor_revision=PREDECESSOR,
        predecessor_activation_receipt_sha256=PREDECESSOR_RECEIPT,
        stage_c_host_artifact_manifest_sha256=host_manifest["manifest_sha256"],
        stage_c_release_update_publication_sha256=publication["publication_sha256"],
        rebind_runtime_sha256=hashlib.sha256(
            harness.runtime_path.read_bytes()
        ).hexdigest(),
        **_owner_runtime_kwargs(),
    )
    return OwnerHarness(
        harness=harness,
        request=request,
        host_manifest_path=host_manifest_path,
        publication_path=publication_path,
        stage0_bundle=FakeStage0Bundle(
            release_root=harness.releases / f"hermes-agent-{TARGET[:12]}",
            host_manifest_sha256=host_manifest["manifest_sha256"],
            publication_sha256=publication["publication_sha256"],
        ),
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

    assert harness.authority["predecessor_sender_release_root"] == str(
        rail.release_root(PREDECESSOR_SENDER)
    )

    terminal = harness.rebind()

    assert terminal["target_revision"] == TARGET
    assert terminal["forward_recovery_performed"] is False
    assert terminal["assert_result"] == {name: "yes" for name in successor.UNIT_NAMES}
    assert harness.host.calls.count(("stop", *successor.TIMER_NAMES)) == 1
    assert ("start", *successor.SERVICE_NAMES) in harness.host.calls
    assert ("start", *successor.TIMER_NAMES) in harness.host.calls
    for name in successor.UNIT_NAMES:
        assert (harness.systemd_root / name).read_bytes() == harness.package.artifacts[
            name
        ]


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


@pytest.mark.parametrize("artifact", ("authority", "preflight"))
def test_rebind_rejects_0600_staged_inputs_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    path = harness.authority_path if artifact == "authority" else harness.preflight_path
    path.chmod(0o600)

    with pytest.raises((
        successor.UpstreamSyncRailSuccessorRebindError,
        activation.UpstreamSyncRailCutoverError,
    )):
        harness.rebind()

    assert harness.host.calls == []


def test_rebind_rejects_wrong_unit_mode_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    (harness.systemd_root / rail.SYNC_SERVICE_UNIT).chmod(0o600)

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="unit_unavailable",
    ):
        harness.rebind()

    assert harness.host.calls == []


def test_rebind_detects_authority_inode_swap_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    original_observe = harness.host.observe
    swapped = False

    def observe_with_swap(
        unit: str,
        *,
        systemd_root: Path,
    ) -> successor.UnitState:
        nonlocal swapped
        observed = original_observe(unit, systemd_root=systemd_root)
        if not swapped:
            replacement = harness.authority_path.with_name("replacement.json")
            _write(
                replacement,
                harness.authority_path.read_bytes(),
                mode=0o444,
            )
            replacement.replace(harness.authority_path)
            swapped = True
        return observed

    monkeypatch.setattr(harness.host, "observe", observe_with_swap)
    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="stage_identity_drifted",
    ):
        harness.rebind()

    assert harness.host.calls == []


def test_rebind_detects_target_unit_inode_swap_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build_harness(tmp_path, monkeypatch)
    original_observe = harness.host.observe
    swapped = False

    def observe_with_target_swap(
        unit: str,
        *,
        systemd_root: Path,
    ) -> successor.UnitState:
        nonlocal swapped
        observed = original_observe(unit, systemd_root=systemd_root)
        target = harness.systemd_root / rail.SYNC_SERVICE_UNIT
        if (
            not swapped
            and hashlib.sha256(target.read_bytes()).hexdigest()
            == harness.authority["target_unit_digests"][rail.SYNC_SERVICE_UNIT]
        ):
            replacement = target.with_name("replacement.service")
            _write(replacement, target.read_bytes(), mode=0o644)
            replacement.replace(target)
            swapped = True
        return observed

    monkeypatch.setattr(harness.host, "observe", observe_with_target_swap)
    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="failed_rolled_back:upstream_sync_successor_unit_identity_drifted",
    ):
        harness.rebind()

    for name, raw in harness.predecessor_units.items():
        assert (harness.systemd_root / name).read_bytes() == raw


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


def test_stage0_drift_after_first_target_write_uses_held_rollback_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)
    changed_units = tuple(
        name
        for name in successor.UNIT_NAMES
        if owner.harness.authority["predecessor_unit_digests"][name]
        != owner.harness.authority["target_unit_digests"][name]
    )

    def drift_after_first_target_write() -> None:
        target_count = sum(
            hashlib.sha256((owner.harness.systemd_root / name).read_bytes()).hexdigest()
            == owner.harness.package.manifest["artifacts"][name]
            for name in changed_units
        )
        if target_count == 1:
            raise successor.release_stage0.ProductionReleaseUpdateStage0Error(
                "release_update_stage0_release_drift"
            )

    owner.stage0_bundle.stable_hook = drift_after_first_target_write

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match=("failed_rolled_back:upstream_sync_successor_stage0_drifted"),
    ):
        owner.apply()

    for name, raw in owner.harness.predecessor_units.items():
        path = owner.harness.systemd_root / name
        assert path.read_bytes() == raw
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    for name in successor.TIMER_NAMES:
        assert owner.harness.host.states[name].enabled_state == "enabled"
        assert owner.harness.host.states[name].active_state == "active"
    authority = activation._read_canonical_json(  # noqa: SLF001
        owner.harness.authority_path,
        root_owned=False,
        modes=frozenset({0o444}),
    )
    rollback = activation._read_canonical_json(  # noqa: SLF001
        successor._transaction_path(  # noqa: SLF001
            authority["authority_sha256"],
            "rollback.json",
            evidence_root=owner.harness.evidence_root,
        ),
        root_owned=False,
        modes=frozenset({0o600}),
    )
    assert rollback["cause"] == "upstream_sync_successor_stage0_drifted"
    assert rollback["rollback_complete"] is True
    assert rollback["target_active"] is False


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
        assert (harness.systemd_root / name).read_bytes() == harness.package.artifacts[
            name
        ]


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
        lambda *args: (
            (1, b"disabled\n")
            if args == ("is-enabled", unit)
            else (_ for _ in ()).throw(AssertionError(args))
        ),
    )

    observed = successor._SystemdHost(root_owned=False).observe(  # noqa: SLF001
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
        lambda *args: (
            (0, b"static\n")
            if args == ("is-enabled", unit)
            else (_ for _ in ()).throw(AssertionError(args))
        ),
    )

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="systemd_observation_invalid",
    ):
        successor._SystemdHost(root_owned=False).observe(  # noqa: SLF001
            unit,
            systemd_root=systemd_root,
        )


def test_systemd_observer_rejects_non_0644_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    systemd_root = tmp_path / "systemd"
    unit = rail.SYNC_SERVICE_UNIT
    _write(systemd_root / unit, b"[Service]\nType=oneshot\n", mode=0o600)
    monkeypatch.setattr(
        activation,
        "_systemctl_property",
        lambda _unit, name: {
            "LoadState": b"loaded\n",
            "FragmentPath": f"{systemd_root / unit}\n".encode(),
        }.get(name, b"\n"),
    )

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="systemd_fragment_invalid",
    ):
        successor._SystemdHost(root_owned=False).observe(  # noqa: SLF001
            unit,
            systemd_root=systemd_root,
        )


def test_owner_path_authors_stages_rebinds_and_replays_without_new_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)

    first = owner.apply()
    calls_after_first = list(owner.harness.host.calls)
    owner.host_manifest_path.unlink()
    owner.publication_path.unlink()
    second = owner.apply()

    assert first == second
    assert first["terminal_verified"] is True
    staged_authority = activation._read_canonical_json(  # noqa: SLF001
        owner.harness.authority_path,
        root_owned=False,
        modes=frozenset({0o444}),
    )
    assert first["authority_sha256"] == staged_authority["authority_sha256"]
    assert owner.harness.authority_path.stat().st_mode & 0o777 == 0o444
    assert owner.harness.preflight_path.stat().st_mode & 0o777 == 0o444
    assert owner.harness.host.calls == calls_after_first


def test_owner_path_recovers_crash_between_exact_authority_and_preflight_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)
    real_stage = successor._stage_create_only  # noqa: SLF001
    calls = 0

    def crash_before_preflight(
        value: dict[str, Any],
        *,
        path: Path,
        root_owned: bool,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        real_stage(value, path=path, root_owned=root_owned)

    monkeypatch.setattr(successor, "_stage_create_only", crash_before_preflight)
    with pytest.raises(KeyboardInterrupt):
        owner.apply()
    assert owner.harness.authority_path.exists()
    assert not owner.harness.preflight_path.exists()
    assert owner.harness.host.calls == []

    monkeypatch.setattr(successor, "_stage_create_only", real_stage)
    result = owner.apply()

    assert result["terminal_verified"] is True
    assert owner.harness.preflight_path.exists()


@pytest.mark.parametrize("stage_name", ("authority", "preflight"))
def test_owner_path_recovers_crash_after_create_only_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_name: str,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)
    target = (
        owner.harness.authority_path
        if stage_name == "authority"
        else owner.harness.preflight_path
    )
    real_unlink = Path.unlink
    crashed = False

    def crash_before_pending_unlink(
        path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal crashed
        if (
            not crashed
            and path.name.startswith(f".{target.name}.")
            and path.name.endswith(".stage")
            and target.exists()
            and path.lstat().st_ino == target.lstat().st_ino
        ):
            crashed = True
            raise KeyboardInterrupt
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", crash_before_pending_unlink)
    with pytest.raises(KeyboardInterrupt):
        owner.apply()

    raw = target.read_bytes()
    pending = successor._stage_pending_path(  # noqa: SLF001
        target,
        hashlib.sha256(raw).hexdigest(),
    )
    assert crashed is True
    assert pending.exists()
    assert target.lstat().st_ino == pending.lstat().st_ino
    assert target.stat().st_nlink == 2
    crash_inode = target.stat().st_ino

    monkeypatch.setattr(Path, "unlink", real_unlink)
    result = owner.apply()

    assert result["terminal_verified"] is True
    assert not pending.exists()
    assert target.stat().st_ino == crash_inode
    assert target.stat().st_nlink == 1


def test_owner_path_holds_stage0_bundle_through_staging_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)

    def reject_after_complete_stage() -> None:
        if (
            owner.harness.authority_path.exists()
            and owner.harness.preflight_path.exists()
        ):
            raise successor.release_stage0.ProductionReleaseUpdateStage0Error(
                "release_update_stage0_release_drift"
            )

    owner.stage0_bundle.stable_hook = reject_after_complete_stage
    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="stage0_invalid",
    ):
        owner.apply()

    assert owner.harness.host.calls == []
    assert not owner.harness.authority_path.exists()
    assert not owner.harness.preflight_path.exists()


def test_owner_path_cleans_first_create_only_inode_on_stage0_drift_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)

    def reject_after_authority_stage() -> None:
        if (
            owner.harness.authority_path.exists()
            and not owner.harness.preflight_path.exists()
        ):
            raise successor.release_stage0.ProductionReleaseUpdateStage0Error(
                "release_update_stage0_release_drift"
            )

    owner.stage0_bundle.stable_hook = reject_after_authority_stage
    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="stage0_invalid",
    ):
        owner.apply()

    assert not owner.harness.authority_path.exists()
    assert not owner.harness.preflight_path.exists()
    assert owner.harness.host.calls == []

    owner.stage0_bundle.stable_hook = None
    assert owner.apply()["terminal_verified"] is True


@pytest.mark.parametrize(
    "field",
    (
        "predecessor_trust_revision",
        "publication_predecessor_revision",
        "plan_predecessor_revision",
    ),
)
def test_owner_path_binds_every_stage0_predecessor_lineage_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)
    setattr(owner.stage0_bundle, field, "8" * 40)

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="stage0_binding_invalid",
    ):
        owner.apply()

    assert not owner.harness.authority_path.exists()
    assert not owner.harness.preflight_path.exists()
    assert owner.harness.host.calls == []


def test_owner_path_foreign_unit_drift_has_no_stage_or_runtime_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)
    _write(
        owner.harness.systemd_root / rail.SYNC_SERVICE_UNIT,
        b"foreign-before-owner-preflight\n",
        mode=0o644,
    )

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="predecessor_unit_invalid",
    ):
        owner.apply()

    assert not owner.harness.authority_path.exists()
    assert not owner.harness.preflight_path.exists()
    assert owner.harness.host.calls == []


@pytest.mark.parametrize(
    "failure",
    (
        "release_update_inputs_document_set_invalid",
        "release_update_contract_signature_invalid",
        "release_update_contract_approval_expired",
    ),
    ids=("truncated", "unsigned", "expired"),
)
def test_owner_path_rejects_noncanonical_stage0_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)

    def rejected_stage0(**_kwargs: Any) -> Any:
        raise successor.release_stage0.ProductionReleaseUpdateStage0Error(failure)

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="stage0_invalid",
    ):
        owner.apply(stage0_verifier=rejected_stage0)

    assert not owner.harness.authority_path.exists()
    assert not owner.harness.preflight_path.exists()
    assert owner.harness.host.calls == []


def test_owner_path_rejects_runtime_digest_drift_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _build_owner_harness(tmp_path, monkeypatch)
    owner.harness.runtime_path.write_bytes(b"foreign-runtime\n")

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="runtime_invalid",
    ):
        owner.apply()

    assert not owner.harness.authority_path.exists()
    assert not owner.harness.preflight_path.exists()
    assert owner.harness.host.calls == []


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "mode"))
def test_create_only_stage_rejects_foreign_file_identity(
    tmp_path: Path,
    kind: str,
) -> None:
    parent = tmp_path / "stage"
    parent.mkdir(mode=0o700)
    target = parent / "authority.json"
    value = {"schema": "exact.test.v1", "secret_material_recorded": False}
    raw = activation._canonical(value) + b"\n"  # noqa: SLF001
    foreign = parent / "foreign"
    foreign.write_bytes(raw)
    foreign.chmod(0o444)
    if kind == "symlink":
        target.symlink_to(foreign)
    elif kind == "hardlink":
        target.hardlink_to(foreign)
    else:
        target.write_bytes(raw)
        target.chmod(0o600)

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="stage_(invalid|hardlink_invalid)",
    ):
        successor._stage_create_only(  # noqa: SLF001
            value,
            path=target,
            root_owned=False,
        )

    assert target.exists() or target.is_symlink()


def test_create_only_stage_rejects_non_root_owner_when_root_is_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "stage"
    parent.mkdir(mode=0o700)
    target = parent / "authority.json"
    value = {"schema": "exact.test.v1", "secret_material_recorded": False}
    _write_canonical(target, value)
    monkeypatch.setattr(
        activation,
        "_validate_trusted_parent_chain",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="stage_invalid",
    ):
        successor._stage_create_only(  # noqa: SLF001
            value,
            path=target,
            root_owned=True,
        )


@pytest.mark.parametrize("after_link", (False, True))
def test_create_only_stage_recovers_its_exact_pending_inode(
    tmp_path: Path,
    after_link: bool,
) -> None:
    parent = tmp_path / "stage"
    parent.mkdir(mode=0o700)
    target = parent / "authority.json"
    value = {"schema": "exact.test.v1", "secret_material_recorded": False}
    raw = activation._canonical(value) + b"\n"  # noqa: SLF001
    digest = hashlib.sha256(raw).hexdigest()
    pending = successor._stage_pending_path(target, digest)  # noqa: SLF001
    pending.write_bytes(raw)
    pending.chmod(0o444)
    if after_link:
        target.hardlink_to(pending)

    successor._stage_create_only(  # noqa: SLF001
        value,
        path=target,
        root_owned=False,
    )

    assert target.read_bytes() == raw
    assert target.stat().st_nlink == 1
    assert not pending.exists()


def test_stage_cleanup_never_removes_same_bytes_on_a_different_inode(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "stage"
    parent.mkdir(mode=0o700)
    target = parent / "authority.json"
    value = {"schema": "exact.test.v1", "secret_material_recorded": False}
    identity = successor._stage_create_only(  # noqa: SLF001
        value,
        path=target,
        root_owned=False,
    )
    raw = target.read_bytes()
    target.unlink()
    _write(target, raw, mode=0o444)

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="stage_cleanup_failed",
    ):
        successor._remove_just_created_stage(  # noqa: SLF001
            value,
            path=target,
            identity=identity,
            root_owned=False,
        )

    assert target.read_bytes() == raw


@pytest.mark.parametrize("same_bytes", (False, True), ids=("foreign", "same-bytes"))
def test_stage_cleanup_restores_swap_after_read_without_deleting_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_bytes: bool,
) -> None:
    parent = tmp_path / "stage"
    parent.mkdir(mode=0o700)
    target = parent / "authority.json"
    value = {"schema": "exact.test.v1", "secret_material_recorded": False}
    identity = successor._stage_create_only(  # noqa: SLF001
        value,
        path=target,
        root_owned=False,
    )
    expected_raw = target.read_bytes()
    replacement_raw = expected_raw if same_bytes else b"foreign replacement\n"
    real_rename = successor._rename_noreplace  # noqa: SLF001
    swapped = False

    def swap_then_rename(source: Path, destination: Path) -> None:
        nonlocal swapped
        if source == target and not swapped:
            swapped = True
            source.unlink()
            _write(source, replacement_raw, mode=0o444)
        real_rename(source, destination)

    monkeypatch.setattr(successor, "_rename_noreplace", swap_then_rename)

    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="stage_cleanup_failed",
    ):
        successor._remove_just_created_stage(  # noqa: SLF001
            value,
            path=target,
            identity=identity,
            root_owned=False,
        )

    assert swapped is True
    assert target.read_bytes() == replacement_raw
    assert not tuple(parent.glob(f".{target.name}.cleanup-quarantine.*"))


def test_owner_request_frame_and_transport_are_closed_to_manual_fields() -> None:
    request = successor.build_owner_request(
        target_revision=TARGET,
        target_package_manifest_sha256="1" * 64,
        predecessor_revision=PREDECESSOR,
        predecessor_activation_receipt_sha256=PREDECESSOR_RECEIPT,
        stage_c_host_artifact_manifest_sha256="2" * 64,
        stage_c_release_update_publication_sha256="3" * 64,
        rebind_runtime_sha256="4" * 64,
        **_owner_runtime_kwargs(),
    )
    assert (
        successor.decode_owner_request(successor.encode_owner_request(request))
        == request
    )
    foreign = dict(request)
    foreign["command"] = "systemctl restart anything"
    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="owner_request_invalid",
    ):
        successor.encode_owner_request(foreign)
    foreign_sender = dict(request)
    foreign_sender["predecessor_sender_revision"] = PREDECESSOR_SENDER
    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="owner_request_invalid",
    ):
        successor.encode_owner_request(foreign_sender)

    class FakeTransport:
        def invoke_successor_rebind(
            self,
            *,
            target_revision: str,
            request_frame: bytes,
        ) -> bytes:
            checked = successor.decode_owner_request(request_frame)
            assert target_revision == TARGET
            result = successor._owner_result(  # noqa: SLF001
                request=checked,
                authority={"authority_sha256": "5" * 64},
                preflight_value={"receipt_sha256": "6" * 64},
                terminal={"receipt_sha256": "7" * 64},
            )
            return activation._canonical(result) + b"\n"  # noqa: SLF001

    result = successor.owner_run(request, transport=FakeTransport())
    assert result["request_sha256"] == request["request_sha256"]
    assert result["caller_selected_commands_allowed"] is False


def test_production_transport_keeps_controller_and_remote_runtime_proofs_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = successor.build_owner_request(
        target_revision=TARGET,
        target_package_manifest_sha256="1" * 64,
        predecessor_revision=PREDECESSOR,
        predecessor_activation_receipt_sha256=PREDECESSOR_RECEIPT,
        stage_c_host_artifact_manifest_sha256="2" * 64,
        stage_c_release_update_publication_sha256="3" * 64,
        rebind_runtime_sha256="4" * 64,
        **_owner_runtime_kwargs(),
    )
    local = {
        name: request[f"controller_owner_runtime_{name}"]
        for name in (
            "manifest_sha256",
            "attestation_sha256",
            "tree_sha256",
            "interpreter_sha256",
        )
    }
    monkeypatch.setattr(
        production_owner_runtime,
        "require_active_owner_runtime",
        lambda revision: local if revision == TARGET else pytest.fail(revision),
    )

    class Identity:
        def account_for_read_only_preflight(self) -> str:
            return "owner@example.invalid"

    calls: list[tuple[str, ...]] = []

    class Transport:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def _run_remote_input(
            self,
            argv: tuple[str, ...],
            **_kwargs: Any,
        ) -> Any:
            calls.append(argv)
            return type("Completed", (), {"stdout": b"exact\n"})()

    monkeypatch.setattr(
        owner_launcher,
        "build_production_cutover_owner_identity",
        lambda revision: (
            (
                Identity(),
                Path("/usr/bin/gcloud"),
                "production-owner",
            )
            if revision == TARGET
            else pytest.fail(revision)
        ),
    )
    monkeypatch.setattr(owner_launcher, "ProductionCutoverTransport", Transport)
    monkeypatch.setattr(
        successor._ProductionOwnerRebindTransport,  # noqa: SLF001
        "_prepare_build_promote",
        lambda _self, _request: None,
    )

    transport = successor._ProductionOwnerRebindTransport(request)  # noqa: SLF001
    assert (
        transport.invoke_successor_rebind(
            target_revision=TARGET,
            request_frame=successor.encode_owner_request(request),
        )
        == b"exact\n"
    )
    assert calls == [
        (
            str(successor.FOUNDATION_V4_WRAPPER),
            TARGET,
            "successor-rebind-owner-apply",
            successor.FOUNDATION_V4_WRAPPER_SHA256,
            request["remote_owner_runtime_manifest_sha256"],
            request["remote_owner_runtime_tree_sha256"],
            request["remote_owner_runtime_interpreter_sha256"],
            request["remote_owner_runtime_attestation_sha256"],
            successor.PREEXEC_VERIFIER_SHA256,
        )
    ]
    assert request["remote_owner_runtime_manifest_sha256"] != local["manifest_sha256"]


def test_production_transport_runs_fixed_prepare_build_promote_before_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = successor.build_owner_request(
        target_revision=TARGET,
        target_package_manifest_sha256="1" * 64,
        predecessor_revision=PREDECESSOR,
        predecessor_activation_receipt_sha256=PREDECESSOR_RECEIPT,
        stage_c_host_artifact_manifest_sha256="2" * 64,
        stage_c_release_update_publication_sha256="3" * 64,
        rebind_runtime_sha256="4" * 64,
        **_owner_runtime_kwargs(),
    )
    local = {
        name: request[f"controller_owner_runtime_{name}"]
        for name in (
            "manifest_sha256",
            "attestation_sha256",
            "tree_sha256",
            "interpreter_sha256",
        )
    }
    monkeypatch.setattr(
        production_owner_runtime,
        "require_active_owner_runtime",
        lambda revision: local if revision == TARGET else pytest.fail(revision),
    )

    class Identity:
        def account_for_read_only_preflight(self) -> str:
            return "owner@example.invalid"

    staging_base = foundation_runtime.RELEASE_BASE / f".{TARGET}.builder-staging"
    built = {
        "publication_sha256": request[
            "remote_owner_runtime_staging_publication_sha256"
        ],
        "manifest_sha256": request["remote_owner_runtime_staging_manifest_sha256"],
        "attestation_sha256": request[
            "remote_owner_runtime_staging_attestation_sha256"
        ],
        "tree_sha256": request["remote_owner_runtime_staging_tree_sha256"],
        "interpreter_sha256": request[
            "remote_owner_runtime_staging_interpreter_sha256"
        ],
        "pyvenv_cfg_sha256": request["remote_owner_runtime_staging_pyvenv_cfg_sha256"],
        "owner_runtime_builder_receipt_sha256": request[
            "remote_owner_runtime_builder_receipt_sha256"
        ],
        "owner_runtime_wheel_sha256": request["remote_owner_runtime_wheel_sha256"],
        "source_tree_oid": SOURCE_TREE,
        "stage_c_builder_terminal_receipt_sha256": STAGE_C_BUILDER_RECEIPT,
    }
    promoted = {
        "publication_sha256": request["remote_owner_runtime_publication_sha256"],
        "manifest_sha256": request["remote_owner_runtime_manifest_sha256"],
        "attestation_sha256": request["remote_owner_runtime_attestation_sha256"],
        "tree_sha256": request["remote_owner_runtime_tree_sha256"],
        "interpreter_sha256": request["remote_owner_runtime_interpreter_sha256"],
    }
    frame = {"schema": "fixed-promotion-frame"}
    monkeypatch.setattr(
        foundation_runtime,
        "validate_staging_publication",
        lambda value, **_kwargs: value,
    )
    monkeypatch.setattr(
        foundation_runtime,
        "validate_publication",
        lambda value, **_kwargs: value,
    )
    monkeypatch.setattr(
        foundation_runtime,
        "build_promotion_frame",
        lambda **_kwargs: frame,
    )
    monkeypatch.setattr(
        owner_launcher,
        "build_production_cutover_owner_identity",
        lambda revision: (
            (Identity(), Path("/usr/bin/gcloud"), "production-owner")
            if revision == TARGET
            else pytest.fail(revision)
        ),
    )

    calls: list[tuple[tuple[str, ...], bytes]] = []
    outputs = [
        activation._canonical({"staging_base": str(staging_base)}) + b"\n",  # noqa: SLF001
        activation._canonical(built) + b"\n",  # noqa: SLF001
        activation._canonical(promoted) + b"\n",  # noqa: SLF001
        b"exact-rebind-result\n",
    ]

    class Transport:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def _run_remote_input(
            self,
            argv: tuple[str, ...],
            *,
            input_bytes: bytes,
            **_kwargs: Any,
        ) -> Any:
            calls.append((argv, input_bytes))
            return type("Completed", (), {"stdout": outputs[len(calls) - 1]})()

    monkeypatch.setattr(owner_launcher, "ProductionCutoverTransport", Transport)

    transport = successor._ProductionOwnerRebindTransport(request)  # noqa: SLF001
    assert (
        transport.invoke_successor_rebind(
            target_revision=TARGET,
            request_frame=successor.encode_owner_request(request),
        )
        == b"exact-rebind-result\n"
    )

    foundation_prefix = (
        str(successor.SUCCESSOR_RUNTIME_FOUNDATION_WRAPPER),
        TARGET,
        request["successor_runtime_foundation_wrapper_sha256"],
        request["successor_runtime_foundation_launcher_sha256"],
        request["successor_runtime_controller_manifest_file_sha256"],
    )
    assert [call[0] for call in calls[:3]] == [
        (foundation_prefix[0], "prepare-runtime", *foundation_prefix[1:]),
        (
            foundation_prefix[0],
            "build-runtime-as-dedicated-builder",
            *foundation_prefix[1:],
            SOURCE_TREE,
            STAGE_C_BUILDER_RECEIPT,
        ),
        (foundation_prefix[0], "promote-runtime", *foundation_prefix[1:]),
    ]
    assert calls[0][1] == b""
    assert calls[1][1] == b""
    assert calls[2][1] == activation._canonical(frame) + b"\n"  # noqa: SLF001


def test_production_transport_rejects_controller_proof_mismatch_without_using_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = successor.build_owner_request(
        target_revision=TARGET,
        target_package_manifest_sha256="1" * 64,
        predecessor_revision=PREDECESSOR,
        predecessor_activation_receipt_sha256=PREDECESSOR_RECEIPT,
        stage_c_host_artifact_manifest_sha256="2" * 64,
        stage_c_release_update_publication_sha256="3" * 64,
        rebind_runtime_sha256="4" * 64,
        **_owner_runtime_kwargs(),
    )
    monkeypatch.setattr(
        production_owner_runtime,
        "require_active_owner_runtime",
        lambda _revision: {
            "manifest_sha256": request["remote_owner_runtime_manifest_sha256"],
            "attestation_sha256": request[
                "controller_owner_runtime_attestation_sha256"
            ],
            "tree_sha256": request["controller_owner_runtime_tree_sha256"],
            "interpreter_sha256": request[
                "controller_owner_runtime_interpreter_sha256"
            ],
        },
    )
    with pytest.raises(
        successor.UpstreamSyncRailSuccessorRebindError,
        match="owner_runtime_identity_invalid",
    ):
        successor._ProductionOwnerRebindTransport(request)  # noqa: SLF001


def test_owner_cli_has_no_path_command_json_or_secret_input() -> None:
    help_text = successor._parser().format_help()  # noqa: SLF001
    for option in (
        "--path",
        "--output",
        "--command",
        "--json",
        "--secret",
        "--ssh",
    ):
        assert option not in help_text
