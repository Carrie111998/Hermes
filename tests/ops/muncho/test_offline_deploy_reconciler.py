from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import status as gateway_status
from ops.muncho.runtime.offline_recovery_systemd import (
    OfflineRecoverySystemdSpec,
    render_scaffold,
)
from ops.muncho.runtime import muncho_offline_deploy_reconciler as reconciler
from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_owner_launcher as owner
from scripts.canary import production_cutover_unit_input_rotation as rotation
from tests.scripts.canary import (
    test_production_cutover_unit_input_rotation as rotation_tests,
)


TRANSACTION_SHA256 = "a" * 64
CAPABILITY_SHA256 = "b" * 64
MANIFEST_SHA256 = "c" * 64
RECONCILER_SHA256 = "d" * 64
TRANSACTION_ID = "e" * 64


def _marker_manifest() -> dict[str, object]:
    return {
        "drain_marker_template": {
            "action": "drain",
            "requested_at": "2026-07-29T00:00:00Z",
            "principal": "muncho-offline-release:test",
            "suppress_notification": False,
        },
        "transaction_sha256": TRANSACTION_SHA256,
        "drain_mutation_capability_sha256": CAPABILITY_SHA256,
    }


def _marker_raw(
    manifest: dict[str, object],
    *,
    epoch: str = "old-boot:123",
) -> bytes:
    marker = reconciler._drain_marker(
        manifest["drain_marker_template"],
        epoch,
        transaction_sha256=TRANSACTION_SHA256,
        capability_sha256=CAPABILITY_SHA256,
    )
    return reconciler._canonical(marker, newline=True)


def test_republish_accepts_only_exact_same_transaction_marker_at_stale_epoch() -> None:
    manifest = _marker_manifest()
    raw = _marker_raw(manifest)

    assert reconciler._validate_republishable_marker(raw, manifest)["epoch"] == (
        "old-boot:123"
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda marker: marker.update(
            held_transaction_sha256="c" * 64
        ),
        lambda marker: marker.update(
            held_mutation_capability_sha256="d" * 64
        ),
        lambda marker: marker.update(principal="manual-operator"),
        lambda marker: marker.update(action="something-else"),
        lambda marker: marker.pop("epoch"),
    ),
)
def test_republish_rejects_foreign_marker_without_rewriting_it(mutate) -> None:
    manifest = _marker_manifest()
    marker = reconciler._drain_marker(
        manifest["drain_marker_template"],
        "old-boot:123",
        transaction_sha256=TRANSACTION_SHA256,
        capability_sha256=CAPABILITY_SHA256,
    )
    foreign = deepcopy(marker)
    mutate(foreign)
    raw = reconciler._canonical(foreign, newline=True)

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_DRAIN_MARKER_FOREIGN",
    ):
        reconciler._validate_republishable_marker(raw, manifest)

    assert raw == reconciler._canonical(foreign, newline=True)


@pytest.mark.parametrize(
    "raw",
    (
        b"{not-json}\n",
        b"{}\n",
        b"\n",
        b"{}\n\n",
        b"{}",
    ),
)
def test_republish_rejects_malformed_marker(raw: bytes) -> None:
    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_DRAIN_MARKER_FOREIGN",
    ):
        reconciler._validate_republishable_marker(raw, _marker_manifest())


def _scaffold_manifest() -> tuple[dict[str, object], OfflineRecoverySystemdSpec]:
    spec = OfflineRecoverySystemdSpec(
        transaction_id=TRANSACTION_ID,
        manifest_sha256=MANIFEST_SHA256,
        reconciler_sha256=RECONCILER_SHA256,
    )
    manifest = {
        "transaction_id": TRANSACTION_ID,
        "service_unit": spec.gateway_unit,
        "active_link": "/opt/adventico-ai-platform/hermes-agent",
        "reconciler_runtime": {
            "path": str(spec.reconciler_path),
            "byte_sha256": RECONCILER_SHA256,
        },
        "scaffolding_paths": {
            "reconciler": str(spec.reconciler_path),
            "gateway_dropin": str(spec.gateway_drop_in_path),
            "recovery_unit": str(spec.recovery_service_path),
            "recovery_timer": str(spec.recovery_timer_path),
        },
        "_manifest_file_sha256": MANIFEST_SHA256,
    }
    return manifest, spec


def test_reconciler_scaffold_contract_matches_frozen_renderer_exactly() -> None:
    manifest, spec = _scaffold_manifest()

    contract = reconciler._scaffold_contract(
        reconciler.OFFLINE_STATE_ROOT
        / TRANSACTION_ID
        / "transaction-manifest.json",
        manifest,
    )

    assert contract["artifacts"] == {
        str(artifact.path): artifact.content
        for artifact in render_scaffold(spec)
    }
    assert contract["authorize_argv"] == spec.reconciler_argv(
        "authorize-start"
    )
    assert contract["reconcile_argv"] == spec.reconciler_argv(
        "reconcile"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("service_unit", "--root.service"),
        ("service_unit", "other-gateway.service"),
        ("active_link", "/tmp/hermes-agent"),
        ("scaffolding_paths", {}),
    ),
)
def test_scaffold_contract_rejects_noncanonical_addresses(
    field: str,
    value: object,
) -> None:
    manifest, _spec = _scaffold_manifest()
    manifest[field] = value

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_SCAFFOLDING_INVALID",
    ):
        reconciler._scaffold_contract(
            reconciler.OFFLINE_STATE_ROOT
            / TRANSACTION_ID
            / "transaction-manifest.json",
            manifest,
        )


def _exec_ex(argv: tuple[str, ...], flags: str) -> str:
    return (
        "{ "
        f"path={argv[0]} ; "
        f"argv[]={' '.join(argv)} ; "
        f"flags={flags} ; "
        "start_time=[n/a] ; "
        "stop_time=[n/a] ; "
        "pid=0 ; "
        "code=(null) ; "
        "status=0/0 "
        "}"
    )


def test_exec_ex_parser_retains_exact_argv_and_privilege_flags() -> None:
    manifest, spec = _scaffold_manifest()
    contract = reconciler._scaffold_contract(spec.manifest_path, manifest)

    assert reconciler._parse_exec_ex(
        _exec_ex(contract["authorize_argv"], "fully-privileged")
    ) == {
        "argv": contract["authorize_argv"],
        "flags": "fully-privileged",
    }


@pytest.mark.parametrize(
    "value",
    (
        "",
        "{ path=/bin/true ; argv[]=/bin/true ; flags=ignore-failure }",
        (
            "{ path=/bin/true ; argv[]=/bin/true ; flags= ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=x ; "
            "code=(null) ; status=0/0 }"
        ),
        (
            "{ path=/bin/false ; argv[]=/bin/true ; flags= ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
            "code=(null) ; status=0/0 }"
        ),
    ),
)
def test_exec_ex_parser_rejects_malformed_or_ambiguous_values(
    value: str,
) -> None:
    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED",
    ):
        reconciler._parse_exec_ex(value)


def _namespace_fixture(tmp_path, monkeypatch):
    systemd = tmp_path / "etc-systemd"
    systemd_control = tmp_path / "etc-systemd-control"
    run_systemd = tmp_path / "run-systemd"
    usr_systemd = tmp_path / "usr-systemd"
    lib_systemd = tmp_path / "lib-systemd"
    libexec = tmp_path / "libexec"
    state_root = tmp_path / "state"
    for path in (
        systemd,
        systemd_control,
        run_systemd,
        usr_systemd,
        lib_systemd,
        libexec,
        state_root,
    ):
        path.mkdir()
    monkeypatch.setattr(reconciler, "SYSTEMD_ROOT", systemd)
    monkeypatch.setattr(
        reconciler,
        "SYSTEMD_SEARCH_ROOTS",
        (
            systemd_control,
            systemd,
            run_systemd,
            usr_systemd,
            lib_systemd,
        ),
    )
    monkeypatch.setattr(reconciler, "LIBEXEC_ROOT", libexec)
    monkeypatch.setattr(reconciler, "OFFLINE_STATE_ROOT", state_root)

    recovery_service = (
        f"{reconciler.RECOVERY_UNIT_PREFIX}{TRANSACTION_ID}.service"
    )
    recovery_timer = (
        f"{reconciler.RECOVERY_UNIT_PREFIX}{TRANSACTION_ID}.timer"
    )
    reconciler_path = (
        libexec
        / f"{reconciler.RECONCILER_PREFIX}{RECONCILER_SHA256}.py"
    )
    service_path = systemd / recovery_service
    timer_path = systemd / recovery_timer
    dropin_path = (
        systemd
        / "hermes-cloud-gateway.service.d"
        / f"{reconciler.GATEWAY_DROP_IN_PREFIX}{TRANSACTION_ID}.conf"
    )
    dropin_path.parent.mkdir()
    for path in (service_path, timer_path, reconciler_path, dropin_path):
        path.write_bytes(b"x")
    (state_root / TRANSACTION_ID).mkdir()
    contract = {
        "recovery_service": recovery_service,
        "recovery_timer": recovery_timer,
        "reconciler_path": str(reconciler_path),
        "state_directory": str(state_root / TRANSACTION_ID),
        "artifacts": {
            str(service_path): b"x",
            str(timer_path): b"x",
            str(dropin_path): b"x",
        },
    }
    return contract, {
        "systemd": systemd,
        "systemd_control": systemd_control,
        "run_systemd": run_systemd,
        "recovery_service": recovery_service,
        "recovery_timer": recovery_timer,
    }


def _namespace_runner(
    recovery_service: str,
    recovery_timer: str,
    *,
    extra_service_file: str | None = None,
    extra_loaded_service: str | None = None,
):
    def runner(argv, **_kwargs):
        args = tuple(argv[1:])
        if args[0] == "list-unit-files" and "--type=service" in args:
            lines = [f"{recovery_service} enabled enabled"]
            if extra_service_file is not None:
                lines.append(f"{extra_service_file} disabled disabled")
        elif args[0] == "list-unit-files" and "--type=timer" in args:
            lines = [f"{recovery_timer} enabled enabled"]
        elif args[0] == "list-units" and "--type=service" in args:
            lines = [
                f"{recovery_service} loaded inactive dead Recovery"
            ]
            if extra_loaded_service is not None:
                lines.append(
                    f"{extra_loaded_service} loaded failed failed Orphan"
                )
        elif args[0] == "list-units" and "--type=timer" in args:
            lines = [
                f"{recovery_timer} loaded active waiting Recovery"
            ]
        else:
            raise AssertionError(args)
        return SimpleNamespace(
            returncode=0,
            stdout=("\n".join(lines) + "\n").encode(),
        )

    return runner


def test_reserved_namespace_collects_all_states_and_all_roots(
    tmp_path,
    monkeypatch,
) -> None:
    contract, values = _namespace_fixture(tmp_path, monkeypatch)

    observed = reconciler._reserved_namespace(
        contract,
        runner=_namespace_runner(
            values["recovery_service"],
            values["recovery_timer"],
        ),
    )

    assert observed["unit_file_recovery_services"] == [
        values["recovery_service"]
    ]
    assert observed["loaded_recovery_services"] == [
        values["recovery_service"]
    ]


def test_reserved_namespace_rejects_disabled_orphan_in_control_root(
    tmp_path,
    monkeypatch,
) -> None:
    contract, values = _namespace_fixture(tmp_path, monkeypatch)
    orphan = f"{reconciler.RECOVERY_UNIT_PREFIX}{'f' * 64}.service"
    (values["systemd_control"] / orphan).write_bytes(b"orphan")

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID",
    ):
        reconciler._reserved_namespace(
            contract,
            runner=_namespace_runner(
                values["recovery_service"],
                values["recovery_timer"],
                extra_service_file=orphan,
            ),
        )


def test_reserved_namespace_rejects_loaded_failed_orphan_without_file(
    tmp_path,
    monkeypatch,
) -> None:
    contract, values = _namespace_fixture(tmp_path, monkeypatch)
    orphan = f"{reconciler.RECOVERY_UNIT_PREFIX}{'f' * 64}.service"

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID",
    ):
        reconciler._reserved_namespace(
            contract,
            runner=_namespace_runner(
                values["recovery_service"],
                values["recovery_timer"],
                extra_loaded_service=orphan,
            ),
        )


def test_deploy_lock_does_not_chmod_untrusted_existing_file(
    tmp_path,
) -> None:
    path = tmp_path / "deploy.lock"
    path.write_bytes(b"")
    path.chmod(0o644)

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_DEPLOY_LOCK_INVALID",
    ):
        with reconciler._deploy_lock(
            path,
            inherited_fd=None,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
            expected_path=path,
        ):
            raise AssertionError("untrusted lock was accepted")

    assert path.stat().st_mode & 0o777 == 0o644


def test_deploy_lock_creates_and_validates_exact_secure_inode(
    tmp_path,
) -> None:
    path = tmp_path / "deploy.lock"

    with reconciler._deploy_lock(
        path,
        inherited_fd=None,
        trusted_uid=os.geteuid(),
        trusted_gid=os.getegid(),
        expected_path=path,
    ):
        assert path.stat().st_mode & 0o777 == 0o600


def test_inherited_deploy_lock_requires_same_actually_locked_inode(
    tmp_path,
) -> None:
    path = tmp_path / "deploy.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with reconciler._deploy_lock(
            path,
            inherited_fd=descriptor,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
            expected_path=path,
        ):
            pass
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_inherited_deploy_lock_rejects_hard_linked_inode(
    tmp_path,
) -> None:
    path = tmp_path / "deploy.lock"
    alias = tmp_path / "alias.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.link(path, alias)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with pytest.raises(
            reconciler.ReconcileError,
            match="OFFLINE_DEPLOY_DEPLOY_LOCK_INVALID",
        ):
            with reconciler._deploy_lock(
                path,
                inherited_fd=descriptor,
                trusted_uid=os.geteuid(),
                trusted_gid=os.getegid(),
                expected_path=path,
            ):
                raise AssertionError("hard-linked lock was accepted")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _gateway_ack_fixture(tmp_path, monkeypatch):
    epoch = "boot-id:4242"
    marker_template = {
        "action": "drain",
        "requested_at": "2026-07-29T00:00:00Z",
        "principal": "muncho-offline-release:test",
        "suppress_notification": False,
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(reconciler, "_current_epoch", lambda: epoch)
    manifest = {
        "gateway_state_path": str(tmp_path / "gateway_state.json"),
        "gateway_uid": os.geteuid(),
        "gateway_gid": os.getegid(),
        "drain_marker_template": marker_template,
        "transaction_sha256": TRANSACTION_SHA256,
        "drain_mutation_capability_sha256": CAPABILITY_SHA256,
    }
    marker_sha256 = reconciler._sha(
        reconciler._canonical(
            reconciler._drain_marker(
                marker_template,
                epoch,
                transaction_sha256=TRANSACTION_SHA256,
                capability_sha256=CAPABILITY_SHA256,
            ),
            newline=True,
        )
    )
    incarnation = {
        "pid": os.getpid(),
        "process_start_ticks": "4242",
        "systemd_invocation_id": "d" * 32,
    }
    acknowledgment = {
        "marker_sha256": marker_sha256,
        "transaction_sha256": TRANSACTION_SHA256,
        "mutation_capability_sha256": CAPABILITY_SHA256,
        "epoch": epoch,
        "process_start_ticks": incarnation["process_start_ticks"],
        "systemd_invocation_id": incarnation["systemd_invocation_id"],
        "ack_sequence": 3,
    }
    return manifest, incarnation, acknowledgment


def test_gateway_zero_state_accepts_ack_written_by_real_status_writer(
    tmp_path,
    monkeypatch,
) -> None:
    manifest, incarnation, acknowledgment = _gateway_ack_fixture(
        tmp_path,
        monkeypatch,
    )
    gateway_status.write_runtime_status(
        gateway_state="draining",
        active_agents=0,
        active_session_keys=[],
        external_drain_ack=acknowledgment,
    )

    observed = reconciler._read_gateway_zero_state(
        manifest,
        expected_incarnation=incarnation,
    )

    assert observed["external_drain_ack"] == acknowledgment


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("marker_sha256", "e" * 64),
        ("transaction_sha256", "e" * 64),
        ("mutation_capability_sha256", "e" * 64),
        ("epoch", "different-epoch"),
        ("process_start_ticks", "9999"),
        ("systemd_invocation_id", "e" * 32),
    ),
)
def test_gateway_zero_state_rejects_ack_outside_arm_and_marker_binding(
    tmp_path,
    monkeypatch,
    field: str,
    value: str,
) -> None:
    manifest, incarnation, acknowledgment = _gateway_ack_fixture(
        tmp_path,
        monkeypatch,
    )
    acknowledgment[field] = value
    gateway_status.write_runtime_status(
        gateway_state="draining",
        active_agents=0,
        active_session_keys=[],
        external_drain_ack=acknowledgment,
    )

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_DRAIN_ZERO_INVALID",
    ):
        reconciler._read_gateway_zero_state(
            manifest,
            expected_incarnation=incarnation,
        )


def _drain_sample_fixture(
    *,
    index: int,
    sequence: int,
    previous_receipt_sha256: str | None,
) -> dict[str, object]:
    acknowledgment = {
        "marker_sha256": "1" * 64,
        "transaction_sha256": TRANSACTION_SHA256,
        "mutation_capability_sha256": CAPABILITY_SHA256,
        "epoch": "boot-id:4242",
        "process_start_ticks": "4242",
        "systemd_invocation_id": "2" * 32,
        "ack_sequence": sequence,
    }
    state = {
        "gateway_state_byte_sha256": "3" * 64,
        "gateway_state_size": 1024,
        "gateway_state_mode": 0o644,
        "gateway_state_inode": 99,
        "external_drain_ack": acknowledgment,
    }
    unsigned = {
        "schema": reconciler.DRAIN_SAMPLE_SCHEMA,
        "manifest_sha256": MANIFEST_SHA256,
        "sample_index": index,
        "previous_sample_receipt_sha256": previous_receipt_sha256,
        "arm_gateway_receipt_sha256": "4" * 64,
        "gateway_incarnation_sha256": "5" * 64,
        "gateway_state": state,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "receipt_sha256": reconciler._sha(
            reconciler._canonical(unsigned)
        ),
    }


def test_second_drain_sample_requires_strictly_newer_gateway_ack(
    tmp_path,
    monkeypatch,
) -> None:
    first = _drain_sample_fixture(
        index=1,
        sequence=7,
        previous_receipt_sha256=None,
    )
    second_same = _drain_sample_fixture(
        index=2,
        sequence=7,
        previous_receipt_sha256=str(first["receipt_sha256"]),
    )
    samples = {
        "drain-zero-sample-1.json": first,
        "drain-zero-sample-2.json": second_same,
    }

    def read_json_file(path, **_kwargs):
        value = samples[path.name]
        return value, reconciler._canonical(value), SimpleNamespace()

    monkeypatch.setattr(reconciler, "_read_json_file", read_json_file)
    monkeypatch.setattr(
        reconciler,
        "_read_arm_gateway",
        lambda *_args, **_kwargs: {
            "receipt_sha256": "4" * 64,
            "incarnation": {
                "gateway_incarnation_sha256": "5" * 64,
            },
        },
    )
    manifest = {"_manifest_file_sha256": MANIFEST_SHA256}
    manifest_path = tmp_path / "transaction-manifest.json"

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_DRAIN_SAMPLE_INVALID",
    ):
        reconciler._read_drain_sample(
            manifest_path,
            manifest,
            2,
        )

    second_newer = _drain_sample_fixture(
        index=2,
        sequence=8,
        previous_receipt_sha256=str(first["receipt_sha256"]),
    )
    samples["drain-zero-sample-2.json"] = second_newer
    assert reconciler._read_drain_sample(
        manifest_path,
        manifest,
        2,
    )["gateway_state"]["external_drain_ack"]["ack_sequence"] == 8


def _write_empty_cgroup(path, *, populated: int = 0) -> None:
    path.mkdir(parents=True)
    (path / "cgroup.procs").write_bytes(b"")
    (path / "cgroup.events").write_text(
        f"populated {populated}\nfrozen 0\n",
        encoding="ascii",
    )


def test_cgroup_empty_proof_covers_entire_descendant_tree(
    tmp_path,
) -> None:
    root = tmp_path / "cgroup"
    service = root / "system.slice" / "gateway.service"
    _write_empty_cgroup(service)
    _write_empty_cgroup(service / "workers")
    _write_empty_cgroup(service / "workers" / "nested")

    observed = reconciler._read_cgroup_empty(
        "/system.slice/gateway.service",
        cgroup_root=root,
        trusted_uid=os.geteuid(),
        trusted_gid=os.getegid(),
    )

    assert observed["cgroup_present"] is True
    assert observed["cgroup_populated"] == 0
    assert observed["cgroup_node_count"] == 3
    assert [
        node["relative_path"]
        for node in observed["cgroup_nodes"]
    ] == [".", "workers", "workers/nested"]
    assert all(
        node["populated"] == 0
        for node in observed["cgroup_nodes"]
    )


def test_cgroup_empty_proof_rejects_hidden_descendant_process(
    tmp_path,
) -> None:
    root = tmp_path / "cgroup"
    service = root / "system.slice" / "gateway.service"
    _write_empty_cgroup(service)
    child = service / "worker"
    _write_empty_cgroup(child)
    (child / "cgroup.procs").write_text("4242\n", encoding="ascii")

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_STOP_PROOF_INVALID",
    ):
        reconciler._read_cgroup_empty(
            "/system.slice/gateway.service",
            cgroup_root=root,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
        )


def test_cgroup_empty_proof_requires_kernel_populated_zero(
    tmp_path,
) -> None:
    root = tmp_path / "cgroup"
    service = root / "system.slice" / "gateway.service"
    _write_empty_cgroup(service, populated=1)

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_STOP_PROOF_INVALID",
    ):
        reconciler._read_cgroup_empty(
            "/system.slice/gateway.service",
            cgroup_root=root,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
        )


def test_cgroup_empty_proof_rejects_symlinked_descendant(
    tmp_path,
) -> None:
    root = tmp_path / "cgroup"
    service = root / "system.slice" / "gateway.service"
    _write_empty_cgroup(service)
    outside = tmp_path / "outside"
    _write_empty_cgroup(outside)
    (service / "worker").symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_STOP_PROOF_INVALID",
    ):
        reconciler._read_cgroup_empty(
            "/system.slice/gateway.service",
            cgroup_root=root,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
        )


@pytest.mark.parametrize(
    ("scenario", "sample_one", "epoch"),
    (
        ("arm-no-samples-reboot", False, "reboot-epoch:1"),
        ("sample-one-reboot", True, "reboot-epoch:2"),
        ("gateway-crash-before-sample-two", True, "arm-epoch:1"),
    ),
)
def test_stopped_at_entry_crash_paths_abort_and_restart_predecessor(
    tmp_path,
    monkeypatch,
    scenario: str,
    sample_one: bool,
    epoch: str,
) -> None:
    manifest_path = tmp_path / "transaction-manifest.json"
    manifest = {
        "_manifest_file_sha256": MANIFEST_SHA256,
        "deploy_lock_path": str(tmp_path / "deploy.lock"),
        "service_unit": "hermes-cloud-gateway.service",
    }
    reconciler._armed_path(manifest_path).write_text(
        scenario,
        encoding="utf-8",
    )
    if sample_one:
        reconciler._drain_sample_path(
            manifest_path,
            1,
        ).write_text("sample-one", encoding="utf-8")
    arm_gateway = {
        "receipt_sha256": "6" * 64,
        "incarnation": {
            "gateway_incarnation_sha256": "7" * 64,
            "control_group": (
                "/system.slice/hermes-cloud-gateway.service"
            ),
        },
    }
    stopped_observation = {
        "load_state": "loaded",
        "active_state": "inactive",
        "sub_state": "dead",
        "main_pid": 0,
        "control_group": "",
        "arm_control_group": arm_gateway[
            "incarnation"
        ]["control_group"],
        "cgroup": {
            "control_group": arm_gateway[
                "incarnation"
            ]["control_group"],
            "cgroup_present": False,
            "cgroup_procs_sha256": None,
            "cgroup_events_sha256": None,
            "cgroup_populated": None,
            "cgroup_node_count": 0,
            "cgroup_nodes": [],
        },
    }
    written: list[dict[str, object]] = []
    replayed: list[str] = []
    started: list[str] = []

    monkeypatch.setattr(
        reconciler,
        "load_manifest",
        lambda *_args, **_kwargs: manifest,
    )
    monkeypatch.setattr(
        reconciler,
        "_deploy_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        reconciler,
        "_validate_armed",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reconciler,
        "_validate_manifest_bindings",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reconciler,
        "_attest_systemd",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reconciler,
        "_republish_epoch_marker",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        reconciler,
        "_read_terminal",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reconciler,
        "_read_arm_gateway",
        lambda *_args, **_kwargs: arm_gateway,
    )
    monkeypatch.setattr(
        reconciler,
        "_gateway_runtime_state",
        lambda *_args, **_kwargs: "stopped",
    )
    monkeypatch.setattr(
        reconciler,
        "_stopped_gateway_observation",
        lambda *_args, **_kwargs: stopped_observation,
    )
    monkeypatch.setattr(
        reconciler,
        "_sample_drain_zero",
        lambda *_args, **_kwargs: pytest.fail(
            "a stopped gateway must not invent a live drain sample"
        ),
    )
    monkeypatch.setattr(
        reconciler,
        "_run_gateway_stop",
        lambda *_args, **_kwargs: pytest.fail(
            "a stopped gateway must not be stopped again"
        ),
    )
    monkeypatch.setattr(
        reconciler,
        "_read_drain_sample",
        lambda *_args, **_kwargs: pytest.fail(
            "sample two must remain absent on stopped-entry recovery"
        ),
    )
    monkeypatch.setattr(reconciler, "_current_epoch", lambda: epoch)

    def create_or_exact(_path, raw, **_kwargs):
        written.append(dict(reconciler._decode(raw, "test-invalid")))

    monkeypatch.setattr(
        reconciler,
        "_create_or_exact",
        create_or_exact,
    )
    monkeypatch.setattr(
        reconciler,
        "_audit_terminal_presence",
        lambda _manifest: {
            "activation": False,
            "abort": False,
            "final": False,
        },
    )
    monkeypatch.setattr(
        reconciler,
        "_triplet_state",
        lambda _manifest: "predecessor",
    )

    def replay(_path, _manifest, *, request_name, **_kwargs):
        replayed.append(request_name)
        return {"decision": "aborted"}

    monkeypatch.setattr(
        reconciler,
        "_replay_terminal_phase",
        replay,
    )
    monkeypatch.setattr(
        reconciler,
        "_enqueue_gateway_start",
        lambda unit, **_kwargs: started.append(unit),
    )

    result = reconciler.reconcile(
        manifest_path,
        require_root=False,
        inherited_lock_fd=123,
    )

    assert result == {"decision": "aborted"}
    assert replayed == ["abort"]
    assert started == ["hermes-cloud-gateway.service"]
    assert len(written) == 1
    stopped_entry = written[0]
    assert stopped_entry["schema"] == reconciler.STOPPED_ENTRY_SCHEMA
    assert stopped_entry["stopped_entry_epoch_sha256"] == (
        reconciler._sha(epoch.encode("utf-8"))
    )
    assert "second_sample_receipt_sha256" not in stopped_entry
    assert stopped_entry["stopped_observation"] == stopped_observation


def test_stopped_entry_receipt_round_trips_without_sample_two(
    tmp_path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "transaction-manifest.json"
    manifest = {"_manifest_file_sha256": MANIFEST_SHA256}
    unsigned = {
        "schema": reconciler.STOPPED_ENTRY_SCHEMA,
        "manifest_sha256": MANIFEST_SHA256,
        "arm_gateway_receipt_sha256": "6" * 64,
        "stopped_entry_epoch_sha256": "8" * 64,
        "stopped_observation": {
            "active_state": "inactive",
            "sub_state": "dead",
        },
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    receipt = {
        **unsigned,
        "receipt_sha256": reconciler._sha(
            reconciler._canonical(unsigned)
        ),
    }
    monkeypatch.setattr(
        reconciler,
        "_read_json_file",
        lambda *_args, **_kwargs: (
            receipt,
            reconciler._canonical(receipt),
            SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        reconciler,
        "_read_arm_gateway",
        lambda *_args, **_kwargs: {
            "receipt_sha256": "6" * 64,
        },
    )
    monkeypatch.setattr(
        reconciler,
        "_read_drain_sample",
        lambda *_args, **_kwargs: pytest.fail(
            "stopped-entry receipt must not depend on sample two"
        ),
    )

    assert reconciler._read_stop(
        manifest_path,
        manifest,
    ) == receipt


def test_owner_activate_refuses_stopped_entry_without_stable_sample_two(
    tmp_path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "transaction-manifest.json"
    reconciler._armed_path(manifest_path).write_text(
        "armed",
        encoding="utf-8",
    )
    manifest = {
        "_manifest_file_sha256": MANIFEST_SHA256,
        "deploy_lock_path": str(tmp_path / "deploy.lock"),
    }
    stopped_entry = {
        "schema": reconciler.STOPPED_ENTRY_SCHEMA,
        "receipt_sha256": "9" * 64,
    }
    monkeypatch.setattr(
        reconciler,
        "load_manifest",
        lambda *_args, **_kwargs: manifest,
    )
    monkeypatch.setattr(
        reconciler,
        "_deploy_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    for name in (
        "_validate_armed",
        "_validate_manifest_bindings",
        "_attest_systemd",
    ):
        monkeypatch.setattr(
            reconciler,
            name,
            lambda *_args, **_kwargs: None,
        )
    monkeypatch.setattr(
        reconciler,
        "_republish_epoch_marker",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        reconciler,
        "_read_terminal",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reconciler,
        "_ensure_gateway_stopped",
        lambda *_args, **_kwargs: (True, stopped_entry),
    )
    monkeypatch.setattr(
        reconciler,
        "_audit_terminal_presence",
        lambda _manifest: {
            "activation": False,
            "abort": False,
            "final": False,
        },
    )
    monkeypatch.setattr(
        reconciler,
        "_triplet_state",
        lambda _manifest: "predecessor",
    )
    monkeypatch.setattr(
        reconciler,
        "_active_target",
        lambda _manifest: "predecessor",
    )
    monkeypatch.setattr(
        reconciler,
        "_replay_terminal_phase",
        lambda *_args, **_kwargs: pytest.fail(
            "stopped-entry proof must never authorize owner finalize"
        ),
    )
    monkeypatch.setattr(
        reconciler,
        "_enqueue_gateway_start",
        lambda *_args, **_kwargs: pytest.fail(
            "rejected owner activation must not enqueue gateway start"
        ),
    )

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_ACTIVATE_PRECONDITION_INVALID",
    ):
        reconciler.activate_finalize(
            manifest_path,
            require_root=False,
            inherited_lock_fd=123,
        )


def _canonical_json(path: Path) -> dict[str, object]:
    return dict(json.loads(path.read_bytes()))


def _mechanical_release_fixture(
    monkeypatch,
    tmp_path,
    *,
    action: str,
) -> dict[str, object]:
    now = 1_900_000_000
    _private, _predecessor_documents, trusted, documents = (
        rotation_tests._release_rotation_state(
            monkeypatch,
            tmp_path,
            now=now,
        )
    )
    prepared = rotation_tests._prepare_release(
        documents,
        trusted,
        now=now,
    )
    preauthorization = rotation_tests._preauthorize_release(
        documents,
        trusted,
        prepared,
        now=now,
    )
    request = dict(
        owner.build_release_unit_input_phase_request(
            action=action,
            owner_release_revision=prepared["predecessor"]["revision"],
            remote_stager_revision=prepared["successor"]["revision"],
            unit_input_publication=documents["publication"],
            release_update_publication=documents["update_publication"],
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            prepared_receipt=prepared,
            preauthorization_receipt=preauthorization,
            expected_transaction_sha256=prepared["transaction_sha256"],
        )
    )
    audit_path = Path(prepared["audit_transaction_path"])
    audited = {
        "transaction.json": _canonical_json(
            audit_path / rotation.TRANSACTION_FILE_NAME
        ),
        "successor-publication.json": _canonical_json(
            audit_path / rotation.PUBLICATION_FILE_NAME
        ),
        "successor-release-update-publication.json": _canonical_json(
            audit_path / rotation.RELEASE_UPDATE_PUBLICATION_FILE_NAME
        ),
        "predecessor-trust.json": _canonical_json(
            audit_path / rotation.PREDECESSOR_TRUST_FILE_NAME
        ),
        "prepared-receipt.json": _canonical_json(
            audit_path / rotation.PREPARED_RECEIPT_FILE_NAME
        ),
        "mutation-begin.json": _canonical_json(
            audit_path / rotation.MUTATION_BEGIN_FILE_NAME
        ),
    }
    logical_files = {
        "plan": package.STAGED_UNIT_INPUT_PLAN_PATH.name,
        "approval": package.STAGED_UNIT_INPUT_APPROVAL_PATH.name,
        "fixed": package.FIXED_UNIT_INPUTS_PATH.name,
    }
    live_paths = {
        "plan": Path(prepared["live_plan_path"]),
        "approval": Path(prepared["live_approval_path"]),
        "fixed": Path(prepared["live_fixed_inputs_path"]),
    }
    predecessor = {
        logical: (
            audit_path
            / rotation.PREDECESSOR_DIRECTORY_NAME
            / filename
        ).read_bytes()
        for logical, filename in logical_files.items()
    }
    successor = {
        "plan": reconciler._canonical(documents["plan"]),
        "approval": reconciler._canonical(documents["approval"]),
        "fixed": reconciler._canonical(
            documents["fixed"],
            newline=True,
        ),
    }
    modes = {"plan": 0o400, "approval": 0o400, "fixed": 0o444}
    predecessor_rows = [
        {
            "logical": logical,
            "live_path": str(live_paths[logical]),
            "audit_path": str(
                audit_path
                / rotation.PREDECESSOR_DIRECTORY_NAME
                / logical_files[logical]
            ),
            "byte_sha256": reconciler._sha(predecessor[logical]),
            "size": len(predecessor[logical]),
            "mode": modes[logical],
        }
        for logical in ("plan", "approval", "fixed")
    ]
    successor_rows = [
        {
            "logical": logical,
            "live_path": str(live_paths[logical]),
            "byte_sha256": reconciler._sha(successor[logical]),
            "mode": modes[logical],
        }
        for logical in ("plan", "approval", "fixed")
    ]
    manifest = {
        "audit_transaction_path": str(audit_path),
        "owner_release_revision": prepared["predecessor"]["revision"],
        "target_revision": prepared["successor"]["revision"],
        "transaction_sha256": prepared["transaction_sha256"],
        "prepared_receipt_sha256": prepared["receipt_sha256"],
        "mutation_begin_sha256": preauthorization[
            "mutation_begin_sha256"
        ],
        "successor_publication_sha256": documents["publication"][
            "publication_sha256"
        ],
        "release_update_publication_sha256": documents[
            "update_publication"
        ]["publication_sha256"],
        "successor_fixed_inputs_sha256": documents["fixed"][
            "fixed_inputs_sha256"
        ],
        "successor_fixed_inputs_file_sha256": reconciler._sha(
            successor["fixed"]
        ),
        "predecessor_triplet": predecessor_rows,
        "successor_triplet": successor_rows,
    }
    return {
        "manifest": manifest,
        "request": request,
        "audited": audited,
        "trusted": trusted,
        "documents": documents,
        "prepared": prepared,
        "preauthorization": preauthorization,
        "predecessor": predecessor,
        "successor": successor,
        "audit_path": audit_path,
    }


def test_mechanical_finalize_is_byte_exact_with_canonical_writer(
    monkeypatch,
    tmp_path,
) -> None:
    fixture = _mechanical_release_fixture(
        monkeypatch,
        tmp_path,
        action=reconciler.FINALIZE_ACTION,
    )
    manifest = fixture["manifest"]
    request = fixture["request"]
    successor = reconciler._derive_successor_triplet_bytes(
        manifest,
        request,
    )

    assert successor == fixture["successor"]
    activation = reconciler._activation_begin_value(manifest)
    canonical_receipt = (
        rotation_tests._finalize_preauthorized_release(
            fixture["documents"],
            fixture["trusted"],
            fixture["prepared"],
            fixture["preauthorization"],
        )
    )
    observed_activation = _canonical_json(
        fixture["audit_path"] / rotation.ACTIVATION_BEGIN_FILE_NAME
    )

    assert activation == observed_activation
    assert reconciler._final_receipt_value(
        manifest,
        fixture["audited"],
        activation,
    ) == canonical_receipt
    result = reconciler._phase_result_value(
        request,
        receipt=canonical_receipt,
        activation=activation,
    )
    assert rotation.validate_release_unit_input_phase_result(
        reconciler.FINALIZE_ACTION,
        request,
        result,
    ) == result


def test_mechanical_abort_is_byte_exact_with_canonical_writer(
    monkeypatch,
    tmp_path,
) -> None:
    fixture = _mechanical_release_fixture(
        monkeypatch,
        tmp_path,
        action=reconciler.ABORT_ACTION,
    )
    canonical_receipt = rotation_tests._abort_preauthorized_release(
        fixture["documents"],
        fixture["trusted"],
        fixture["prepared"],
        fixture["preauthorization"],
    )

    assert reconciler._abort_receipt_value(
        fixture["manifest"]
    ) == canonical_receipt
    result = reconciler._phase_result_value(
        fixture["request"],
        receipt=canonical_receipt,
        activation=None,
    )
    assert rotation.validate_release_unit_input_phase_result(
        reconciler.ABORT_ACTION,
        fixture["request"],
        result,
    ) == result


def test_terminal_replay_never_runs_candidate_code_and_commits_wal_first(
    monkeypatch,
    tmp_path,
) -> None:
    fixture = _mechanical_release_fixture(
        monkeypatch,
        tmp_path,
        action=reconciler.FINALIZE_ACTION,
    )
    writes: list[str] = []
    converged: list[bool] = []
    monkeypatch.setattr(
        reconciler,
        "_read_bound_request",
        lambda *_args, **_kwargs: (
            fixture["request"],
            reconciler._canonical(fixture["request"]),
        ),
    )
    monkeypatch.setattr(
        reconciler,
        "_replay_documents",
        lambda _manifest: fixture["audited"],
    )
    monkeypatch.setattr(
        reconciler,
        "_predecessor_triplet_bytes",
        lambda _manifest: fixture["predecessor"],
    )
    monkeypatch.setattr(
        reconciler,
        "_authority_activation_lock",
        nullcontext,
    )
    monkeypatch.setattr(
        reconciler,
        "_require_other_audit_transactions_terminal",
        lambda _manifest: None,
    )
    monkeypatch.setattr(
        reconciler,
        "_triplet_parent",
        lambda *_args, **_kwargs: Path(
            fixture["prepared"]["live_plan_path"]
        ).parent,
    )
    monkeypatch.setattr(
        reconciler,
        "_audit_terminal_presence",
        lambda _manifest: {
            "activation": False,
            "abort": False,
            "final": False,
        },
    )

    def create_or_exact(path, _raw, **_kwargs):
        writes.append(path.name)

    def converge(*_args, **_kwargs):
        assert writes == [reconciler.ACTIVATION_FILE]
        converged.append(True)

    monkeypatch.setattr(
        reconciler,
        "_create_or_exact",
        create_or_exact,
    )
    monkeypatch.setattr(
        reconciler,
        "_converge_successor_triplet",
        converge,
    )

    def forbidden_runner(*_args, **_kwargs):
        raise AssertionError("candidate code executed")

    result = reconciler._invoke_request(
        fixture["manifest"],
        "finalize",
        runner=forbidden_runner,
    )

    assert converged == [True]
    assert writes == [reconciler.ACTIVATION_FILE, reconciler.FINAL_FILE]
    assert result["action"] == reconciler.FINALIZE_ACTION


def _triplet_recovery_fixture(
    tmp_path,
    state: dict[str, str | None],
) -> tuple[dict[str, object], dict[str, bytes], dict[str, bytes]]:
    root = tmp_path / "staged"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    names = {
        "plan": "unit-input-plan.json",
        "approval": "unit-input-approval.json",
        "fixed": "production-unit-inputs.json",
    }
    modes = {"plan": 0o400, "approval": 0o400, "fixed": 0o444}
    predecessor = {
        logical: f"predecessor-{logical}".encode()
        for logical in names
    }
    successor = {
        logical: f"successor-{logical}".encode()
        for logical in names
    }
    for logical, selected in state.items():
        if selected is None:
            continue
        path = root / names[logical]
        value = (
            predecessor[logical]
            if selected == "predecessor"
            else successor[logical]
            if selected == "successor"
            else b"foreign"
        )
        path.write_bytes(value)
        path.chmod(modes[logical])
    rows = [
        {
            "logical": logical,
            "live_path": str(root / names[logical]),
            "byte_sha256": reconciler._sha(values[logical]),
            "mode": modes[logical],
        }
        for values in (predecessor, successor)
        for logical in ("plan", "approval", "fixed")
    ]
    return {
        "predecessor_triplet": rows[:3],
        "successor_triplet": rows[3:],
    }, predecessor, successor


@pytest.mark.parametrize(
    "state",
    (
        {
            "plan": "predecessor",
            "approval": "predecessor",
            "fixed": "predecessor",
        },
        {
            "plan": "predecessor",
            "approval": "predecessor",
            "fixed": None,
        },
        {
            "plan": "predecessor",
            "approval": None,
            "fixed": None,
        },
        {"plan": None, "approval": None, "fixed": None},
        {"plan": "successor", "approval": None, "fixed": None},
        {
            "plan": "successor",
            "approval": "successor",
            "fixed": None,
        },
        {
            "plan": "successor",
            "approval": "successor",
            "fixed": "successor",
        },
    ),
)
def test_mechanical_triplet_replay_recovers_every_durable_checkpoint(
    tmp_path,
    state,
) -> None:
    manifest, predecessor, successor = _triplet_recovery_fixture(
        tmp_path,
        state,
    )
    kwargs = {
        "trusted_uid": os.geteuid(),
        "trusted_gid": os.getegid(),
    }

    reconciler._converge_successor_triplet(
        manifest,
        predecessor=predecessor,
        successor=successor,
        **kwargs,
    )
    reconciler._converge_successor_triplet(
        manifest,
        predecessor=predecessor,
        successor=successor,
        **kwargs,
    )

    for row in manifest["successor_triplet"]:
        assert Path(row["live_path"]).read_bytes() == successor[
            row["logical"]
        ]


def test_mechanical_triplet_replay_rejects_unknown_mixed_bytes(
    tmp_path,
) -> None:
    manifest, predecessor, successor = _triplet_recovery_fixture(
        tmp_path,
        {
            "plan": "foreign",
            "approval": "predecessor",
            "fixed": "predecessor",
        },
    )

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED",
    ):
        reconciler._converge_successor_triplet(
            manifest,
            predecessor=predecessor,
            successor=successor,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
        )


def test_parallel_writer_transaction_must_be_exactly_terminal(
    monkeypatch,
    tmp_path,
) -> None:
    fixture = _mechanical_release_fixture(
        monkeypatch,
        tmp_path,
        action=reconciler.FINALIZE_ACTION,
    )
    kwargs = {
        "trusted_uid": os.geteuid(),
        "trusted_gid": os.getegid(),
    }

    reconciler._require_other_audit_transactions_terminal(
        fixture["manifest"],
        **kwargs,
    )

    incomplete = (
        fixture["audit_path"].parent
        / f"{'1' * 64}-{'2' * 64}"
    )
    incomplete.mkdir(mode=0o700)
    incomplete.chmod(0o700)
    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID",
    ):
        reconciler._require_other_audit_transactions_terminal(
            fixture["manifest"],
            **kwargs,
        )


def _receipt(
    unsigned: dict[str, object],
    *,
    field: str = "receipt_sha256",
) -> dict[str, object]:
    return {
        **unsigned,
        field: reconciler._sha(reconciler._canonical(unsigned)),
    }


def _health_contract_fixture() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    revision = "1" * 40
    manifest = {
        "_manifest_file_sha256": MANIFEST_SHA256,
        "transaction_sha256": TRANSACTION_SHA256,
        "drain_mutation_capability_sha256": CAPABILITY_SHA256,
        "service_unit": "hermes-cloud-gateway.service",
    }
    terminal = {
        "decision": "finalized",
        "required_revision": revision,
        "active_link_target": (
            "/opt/adventico-ai-platform/releases/" + revision
        ),
    }
    gateway_unsigned = {
        "service_unit": manifest["service_unit"],
        "pid": 4242,
        "systemd_invocation_id": "2" * 32,
        "process_start_ticks": "987654",
        "runtime_executable": (
            terminal["active_link_target"] + "/.venv/bin/python"
        ),
        "runtime_executable_target": "/usr/bin/python3.13",
        "active_link_target": terminal["active_link_target"],
        "revision": revision,
    }
    gateway = _receipt(
        gateway_unsigned,
        field="gateway_incarnation_sha256",
    )
    binding = {
        "manifest_sha256": MANIFEST_SHA256,
        "gateway_incarnation_sha256": gateway[
            "gateway_incarnation_sha256"
        ],
        "revision": revision,
    }
    safe = {
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    model_probe = _receipt(
        {
            "schema": reconciler.MODEL_HEALTH_SCHEMA,
            **binding,
            "provider": "openai-codex",
            "requested_model": "gpt-5.6-sol",
            "response_model": "gpt-5.6-sol",
            "api_status_code": 200,
            "completed": True,
            "input_tokens": 17,
            "output_tokens": 3,
            "request_id_sha256": "3" * 64,
            "response_id_sha256": "4" * 64,
            **safe,
        }
    )
    secret_manager_probe = _receipt(
        {
            "schema": reconciler.SECRET_MANAGER_HEALTH_SCHEMA,
            **binding,
            "project_id": "adventico-ai-platform",
            "access_principal_sha256": "5" * 64,
            "required_resource_set_sha256": "6" * 64,
            "required_resource_count": 4,
            "access_attempt_count": 4,
            "access_success_count": 4,
            "rpc_status_code": 0,
            "secret_payload_material_recorded": False,
            "secret_payload_digest_recorded": False,
            "secret_version_recorded": False,
            **safe,
        }
    )
    cloud_sql_probe = _receipt(
        {
            "schema": reconciler.CLOUD_SQL_HEALTH_SCHEMA,
            **binding,
            "project_id": "adventico-ai-platform",
            "database": "ai_platform_brain",
            "instance_identity_sha256": "7" * 64,
            "tls_server_name_sha256": "8" * 64,
            "server_ca_sha256": "9" * 64,
            "peer_certificate_spki_sha256": "a" * 64,
            "tls_in_use": True,
            "tls_verify_full": True,
            "tls_protocol": "TLSv1.3",
            "server_version_num": 170_000,
            "backend_pid": 5252,
            "connect_status_code": 0,
            "sqlstate": "00000",
            **safe,
        }
    )
    canonical_query_probe = _receipt(
        {
            "schema": reconciler.CANONICAL_QUERY_HEALTH_SCHEMA,
            **binding,
            "service": "canonical_writer",
            "protocol": "v1",
            "database_identity": "canonical_brain_migration_owner",
            "operation": "case.query",
            "request_id_sha256": "b" * 64,
            "sentinel_case_id_sha256": "c" * 64,
            "result_count": 0,
            "protocol_status_code": 0,
            "read_only": True,
            **safe,
        }
    )
    privileged_writer_probe = _receipt(
        {
            "schema": reconciler.PRIVILEGED_WRITER_HEALTH_SCHEMA,
            **binding,
            "database": "ai_platform_brain",
            "session_user_sha256": "d" * 64,
            "transaction_sha256": "e" * 64,
            "advisory_lock_key_sha256": "f" * 64,
            "inserted_event_id_sha256": "0" * 64,
            "isolation_level": "serializable",
            "advisory_lock_acquired": True,
            "append_only": True,
            "insert_row_count": 1,
            "readback_row_count": 1,
            "update_row_count": 0,
            "delete_row_count": 0,
            "rollback_completed": True,
            "transaction_committed": False,
            "fresh_session_read_count": 0,
            "transaction_sqlstate": "00000",
            "rollback_sqlstate": "00000",
            "fresh_read_sqlstate": "00000",
            **safe,
        }
    )
    request_content_sha256 = "1" * 64
    permit = _receipt(
        {
            "schema": reconciler.DISCORD_PERMIT_SCHEMA,
            "manifest_sha256": MANIFEST_SHA256,
            "transaction_sha256": TRANSACTION_SHA256,
            "mutation_capability_sha256": CAPABILITY_SHA256,
            "epoch_sha256": reconciler._sha(
                b"test-boot-id:987654"
            ),
            "gateway_incarnation_sha256": gateway[
                "gateway_incarnation_sha256"
            ],
            "guild_id": "1282725267068157972",
            "channel_id": "1504852355588423801",
            "request_author_user_id": "1279454038731264061",
            "request_content_sha256": request_content_sha256,
            "probe_nonce_sha256": "2" * 64,
            "maximum_uses": 1,
            "issued_by_root": True,
            **safe,
        }
    )
    request_message_id = "1531409183163813970"
    consumption = _receipt(
        {
            "schema": reconciler.DISCORD_PERMIT_CONSUMPTION_SCHEMA,
            "permit_receipt_sha256": permit["receipt_sha256"],
            "gateway_incarnation_sha256": gateway[
                "gateway_incarnation_sha256"
            ],
            "request_message_id": request_message_id,
            "consumed_uses": 1,
            "remaining_uses": 0,
            "atomic_create": True,
            **safe,
        }
    )
    discord_probe = _receipt(
        {
            "schema": reconciler.DISCORD_HEALTH_SCHEMA,
            **binding,
            "guild_id": permit["guild_id"],
            "channel_id": permit["channel_id"],
            "request_author_user_id": permit[
                "request_author_user_id"
            ],
            "request_message_id": request_message_id,
            "request_content_sha256": request_content_sha256,
            "response_message_id": "1531409183163813971",
            "response_author_id_sha256": "3" * 64,
            "response_content_sha256": "4" * 64,
            "reply_reference_message_id": request_message_id,
            "response_count": 1,
            "round_trip_milliseconds": 750,
            "request_delivery_status_code": 0,
            "response_observation_status_code": 0,
            "permit": permit,
            "permit_consumption": consumption,
            **safe,
        }
    )
    final_health = _receipt(
        {
            "schema": reconciler.HEALTH_SCHEMA,
            "manifest_sha256": MANIFEST_SHA256,
            "decision": terminal["decision"],
            "revision": revision,
            "gateway_incarnation": gateway,
            "model_probe": model_probe,
            "secret_manager_probe": secret_manager_probe,
            "cloud_sql_probe": cloud_sql_probe,
            "canonical_query_probe": canonical_query_probe,
            "privileged_writer_probe": privileged_writer_probe,
            "discord_probe": discord_probe,
            **safe,
        }
    )
    return manifest, terminal, final_health


def _resign_health_probe(
    health: dict[str, object],
    probe_name: str,
) -> None:
    probe = dict(health[probe_name])
    probe["receipt_sha256"] = reconciler._sha(
        reconciler._canonical(
            {
                key: value
                for key, value in probe.items()
                if key != "receipt_sha256"
            }
        )
    )
    health[probe_name] = probe
    health["receipt_sha256"] = reconciler._sha(
        reconciler._canonical(
            {
                key: value
                for key, value in health.items()
                if key != "receipt_sha256"
            }
        )
    )


def _validate_health_contract(
    monkeypatch,
    health: dict[str, object],
    *,
    incarnation: dict[str, object] | None = None,
) -> dict[str, object]:
    manifest, terminal, _valid = _health_contract_fixture()
    monkeypatch.setattr(
        reconciler,
        "_current_epoch",
        lambda: "test-boot-id:987654",
    )
    return dict(
        reconciler._validate_final_health(
            health,
            manifest=manifest,
            terminal=terminal,
            incarnation=incarnation,
        )
    )


def test_final_health_accepts_exact_nested_typed_receipts(
    monkeypatch,
) -> None:
    _manifest, _terminal, health = _health_contract_fixture()

    assert _validate_health_contract(monkeypatch, health) == health


@pytest.mark.parametrize(
    ("probe_name", "field", "value"),
    (
        ("model_probe", "api_status_code", 429),
        (
            "secret_manager_probe",
            "access_success_count",
            3,
        ),
        (
            "secret_manager_probe",
            "rpc_status_code",
            False,
        ),
        ("cloud_sql_probe", "tls_verify_full", False),
        ("cloud_sql_probe", "connect_status_code", False),
        ("canonical_query_probe", "result_count", 1),
        ("canonical_query_probe", "result_count", False),
        (
            "privileged_writer_probe",
            "transaction_committed",
            True,
        ),
        (
            "privileged_writer_probe",
            "insert_row_count",
            True,
        ),
        ("discord_probe", "response_count", 2),
    ),
)
def test_final_health_rejects_rehashed_probe_contract_violations(
    monkeypatch,
    probe_name: str,
    field: str,
    value: object,
) -> None:
    _manifest, _terminal, health = _health_contract_fixture()
    probe = dict(health[probe_name])
    probe[field] = value
    health[probe_name] = probe
    _resign_health_probe(health, probe_name)

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_HEALTH_INVALID",
    ):
        _validate_health_contract(monkeypatch, health)


@pytest.mark.parametrize(
    "forbidden_field",
    (
        "secret_payload",
        "secret_value",
        "secret_digest",
        "secret_version",
    ),
)
def test_secret_manager_probe_rejects_secret_bearing_fields(
    monkeypatch,
    forbidden_field: str,
) -> None:
    _manifest, _terminal, health = _health_contract_fixture()
    probe = dict(health["secret_manager_probe"])
    probe[forbidden_field] = "forbidden"
    health["secret_manager_probe"] = probe
    _resign_health_probe(health, "secret_manager_probe")

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_HEALTH_INVALID",
    ):
        _validate_health_contract(monkeypatch, health)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("rollback_completed", False),
        ("transaction_committed", True),
        ("update_row_count", 1),
        ("delete_row_count", 1),
        ("fresh_session_read_count", 1),
    ),
)
def test_privileged_writer_requires_rolled_back_append_only_probe(
    monkeypatch,
    field: str,
    value: object,
) -> None:
    _manifest, _terminal, health = _health_contract_fixture()
    probe = dict(health["privileged_writer_probe"])
    probe[field] = value
    health["privileged_writer_probe"] = probe
    _resign_health_probe(health, "privileged_writer_probe")

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_HEALTH_INVALID",
    ):
        _validate_health_contract(monkeypatch, health)


def _resign_discord(
    health: dict[str, object],
    *,
    permit: bool = False,
    consumption: bool = False,
) -> None:
    probe = dict(health["discord_probe"])
    if permit:
        permit_value = dict(probe["permit"])
        permit_value["receipt_sha256"] = reconciler._sha(
            reconciler._canonical(
                {
                    key: value
                    for key, value in permit_value.items()
                    if key != "receipt_sha256"
                }
            )
        )
        probe["permit"] = permit_value
        consumption_value = dict(probe["permit_consumption"])
        consumption_value["permit_receipt_sha256"] = permit_value[
            "receipt_sha256"
        ]
        probe["permit_consumption"] = consumption_value
        consumption = True
    if consumption:
        consumption_value = dict(probe["permit_consumption"])
        consumption_value["receipt_sha256"] = reconciler._sha(
            reconciler._canonical(
                {
                    key: value
                    for key, value in consumption_value.items()
                    if key != "receipt_sha256"
                }
            )
        )
        probe["permit_consumption"] = consumption_value
    health["discord_probe"] = probe
    _resign_health_probe(health, "discord_probe")


@pytest.mark.parametrize(
    ("container", "field", "value"),
    (
        ("permit", "maximum_uses", 2),
        ("permit", "maximum_uses", True),
        ("permit", "transaction_sha256", "9" * 64),
        ("permit", "epoch_sha256", "8" * 64),
        ("permit_consumption", "consumed_uses", 2),
        ("permit_consumption", "remaining_uses", 1),
        ("permit_consumption", "remaining_uses", False),
        ("permit_consumption", "atomic_create", False),
        ("discord_probe", "reply_reference_message_id", "1" * 18),
        ("discord_probe", "response_count", True),
        ("discord_probe", "request_delivery_status_code", 1),
        ("discord_probe", "response_observation_status_code", 1),
    ),
)
def test_discord_probe_rejects_reuse_and_binding_violations(
    monkeypatch,
    container: str,
    field: str,
    value: object,
) -> None:
    _manifest, _terminal, health = _health_contract_fixture()
    probe = dict(health["discord_probe"])
    if container == "discord_probe":
        probe[field] = value
        health["discord_probe"] = probe
        _resign_health_probe(health, "discord_probe")
    else:
        nested = dict(probe[container])
        nested[field] = value
        probe[container] = nested
        health["discord_probe"] = probe
        _resign_discord(
            health,
            permit=container == "permit",
            consumption=container == "permit_consumption",
        )

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_HEALTH_INVALID",
    ):
        _validate_health_contract(monkeypatch, health)


def test_final_health_rejects_live_gateway_incarnation_mismatch(
    monkeypatch,
) -> None:
    _manifest, _terminal, health = _health_contract_fixture()
    different = dict(health["gateway_incarnation"])
    different["pid"] = 5252
    different["gateway_incarnation_sha256"] = reconciler._sha(
        reconciler._canonical(
            {
                key: value
                for key, value in different.items()
                if key != "gateway_incarnation_sha256"
            }
        )
    )

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_HEALTH_INVALID",
    ):
        _validate_health_contract(
            monkeypatch,
            health,
            incarnation=different,
        )


def test_final_health_rejects_runtime_executable_outside_terminal_target(
    monkeypatch,
) -> None:
    _manifest, _terminal, health = _health_contract_fixture()
    gateway = dict(health["gateway_incarnation"])
    gateway["runtime_executable"] = "/tmp/unbound-python"
    gateway["gateway_incarnation_sha256"] = reconciler._sha(
        reconciler._canonical(
            {
                key: value
                for key, value in gateway.items()
                if key != "gateway_incarnation_sha256"
            }
        )
    )
    health["gateway_incarnation"] = gateway
    health["receipt_sha256"] = reconciler._sha(
        reconciler._canonical(
            {
                key: value
                for key, value in health.items()
                if key != "receipt_sha256"
            }
        )
    )

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_HEALTH_INVALID",
    ):
        _validate_health_contract(monkeypatch, health)


def test_read_health_selects_nested_gateway_incarnation_digest(
    monkeypatch,
    tmp_path,
) -> None:
    manifest, terminal, health = _health_contract_fixture()
    manifest_path = tmp_path / "transaction-manifest.json"
    health_dir = tmp_path / "final-health"
    health_dir.mkdir()
    incarnation_sha256 = health["gateway_incarnation"][
        "gateway_incarnation_sha256"
    ]
    health_path = health_dir / f"{incarnation_sha256}.json"
    health_path.write_bytes(reconciler._canonical(health))
    monkeypatch.setattr(
        reconciler,
        "_directory",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reconciler,
        "_read_json_file",
        lambda *_args, **_kwargs: (
            health,
            reconciler._canonical(health),
            SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        reconciler,
        "_current_epoch",
        lambda: "test-boot-id:987654",
    )

    assert reconciler._read_health(
        manifest_path,
        manifest,
        terminal,
        incarnation_sha256=incarnation_sha256,
        receipt_sha256=health["receipt_sha256"],
    ) == health


def test_read_health_rejects_filename_not_bound_to_nested_incarnation(
    monkeypatch,
    tmp_path,
) -> None:
    manifest, terminal, health = _health_contract_fixture()
    manifest_path = tmp_path / "transaction-manifest.json"
    health_dir = tmp_path / "final-health"
    health_dir.mkdir()
    (health_dir / f"{'9' * 64}.json").write_bytes(
        reconciler._canonical(health)
    )
    monkeypatch.setattr(
        reconciler,
        "_directory",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        reconciler,
        "_read_json_file",
        lambda *_args, **_kwargs: (
            health,
            reconciler._canonical(health),
            SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        reconciler,
        "_current_epoch",
        lambda: "test-boot-id:987654",
    )

    with pytest.raises(
        reconciler.ReconcileError,
        match="OFFLINE_DEPLOY_HEALTH_INVALID",
    ):
        reconciler._read_health(
            manifest_path,
            manifest,
            terminal,
        )
