from __future__ import annotations

import shutil
import subprocess
import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from ops.muncho.runtime.offline_recovery_systemd import (
    CleanedScaffoldObservation,
    CleanupSystemctlPlan,
    EffectiveCommand,
    LoadedScaffoldObservation,
    OfflineRecoverySystemdError,
    OfflineRecoverySystemdSpec,
    ReservedNamespaceObservation,
    UnitArtifact,
    cleanup_systemctl_commands,
    cleanup_readback_commands,
    prearm_readback_commands,
    prearm_systemctl_commands,
    render_gateway_drop_in,
    render_recovery_service,
    render_recovery_timer,
    render_scaffold,
    scaffold_sha256s,
    validate_cleaned_scaffold,
    validate_loaded_scaffold,
    validate_reserved_namespace,
    validate_rendered_scaffold,
)


TX = "1" * 64
MANIFEST = "2" * 64
RECONCILER = "3" * 64


@pytest.fixture
def spec() -> OfflineRecoverySystemdSpec:
    return OfflineRecoverySystemdSpec(
        transaction_id=TX,
        manifest_sha256=MANIFEST,
        reconciler_sha256=RECONCILER,
    )


def _text(artifact: UnitArtifact) -> str:
    return artifact.content.decode("utf-8")


def _observation(
    spec: OfflineRecoverySystemdSpec,
) -> LoadedScaffoldObservation:
    return LoadedScaffoldObservation(
        recovery_service_enabled=True,
        recovery_timer_enabled=True,
        recovery_timer_active=True,
        recovery_service_fragment_path=str(spec.recovery_service_path),
        recovery_timer_fragment_path=str(spec.recovery_timer_path),
        gateway_drop_in_paths=frozenset({
            "/etc/systemd/system/other.conf",
            str(spec.gateway_drop_in_path),
        }),
        gateway_wants=frozenset({"network-online.target", spec.recovery_service}),
        gateway_after=frozenset({"basic.target", spec.recovery_service}),
        gateway_exec_conditions=(spec.expected_gateway_exec_condition,),
        recovery_before=frozenset({spec.gateway_unit, "shutdown.target"}),
        recovery_drop_in_paths=frozenset(),
        recovery_exec_starts=(spec.expected_recovery_exec_start,),
        timer_drop_in_paths=frozenset(),
        timer_triggers=frozenset({spec.recovery_service}),
    )


def test_content_addressed_paths_are_absolute_and_transaction_scoped(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    assert spec.state_directory == PurePosixPath(
        f"/var/lib/muncho-offline-release-transactions/{TX}"
    )
    assert spec.manifest_path == (spec.state_directory / "transaction-manifest.json")
    assert spec.reconciler_path == PurePosixPath(
        f"/usr/local/libexec/muncho-offline-release-reconcile-{RECONCILER}.py"
    )
    assert spec.recovery_service_path.is_absolute()
    assert spec.recovery_timer_path.is_absolute()
    assert spec.gateway_drop_in_path.is_absolute()
    assert TX in spec.recovery_service
    assert TX in spec.recovery_timer
    assert TX in spec.gateway_drop_in_path.name


def test_gateway_drop_in_has_one_exact_isolated_exec_condition(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    text = _text(render_gateway_drop_in(spec))
    lines = text.splitlines()
    conditions = [line for line in lines if line.startswith("ExecCondition=")]

    assert conditions == [
        "ExecCondition=+/usr/bin/python3 -I -S -B "
        f"/usr/local/libexec/muncho-offline-release-reconcile-{RECONCILER}.py "
        "authorize-start "
        f"--manifest=/var/lib/muncho-offline-release-transactions/{TX}/"
        "transaction-manifest.json "
        f"--manifest-sha256={MANIFEST}"
    ]
    assert "ExecCondition=" not in {line for line in lines if line == "ExecCondition="}
    assert f"Wants={spec.recovery_service}" in lines
    assert f"After={spec.recovery_service}" in lines
    assert "/bin/sh" not in text
    assert "/bin/bash" not in text
    assert " -c " not in text
    assert "/opt/adventico-ai-platform/hermes-agent/.venv" not in text
    assert "$" not in text
    assert "%" not in text


def test_recovery_service_is_boot_ordered_without_gateway_cycle_or_network(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    text = _text(render_recovery_service(spec))
    lines = text.splitlines()
    exec_starts = [line for line in lines if line.startswith("ExecStart=")]

    assert "DefaultDependencies=no" in lines
    assert "Requires=local-fs.target" in lines
    assert "After=local-fs.target" in lines
    assert f"Before={spec.gateway_unit} shutdown.target" in lines
    assert "Conflicts=shutdown.target" in lines
    assert "WantedBy=multi-user.target" in lines
    assert exec_starts == [
        "ExecStart=/usr/bin/python3 -I -S -B "
        f"/usr/local/libexec/muncho-offline-release-reconcile-{RECONCILER}.py "
        "reconcile "
        f"--manifest=/var/lib/muncho-offline-release-transactions/{TX}/"
        "transaction-manifest.json "
        f"--manifest-sha256={MANIFEST}"
    ]
    assert f"After={spec.gateway_unit}" not in lines
    assert f"Requires={spec.gateway_unit}" not in lines
    assert "network.target" not in text
    assert "network-online.target" not in text
    assert "ExecStartPost=" not in text
    assert "/usr/bin/systemctl" not in text
    assert "Environment=PYTHON" not in text
    assert "/bin/sh" not in text
    assert "/bin/bash" not in text
    assert " -c " not in text
    assert "$" not in text
    assert "%" not in text


def test_retry_timer_targets_oneshot_and_is_boot_enabled(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    text = _text(render_recovery_timer(spec))
    lines = text.splitlines()

    assert "OnActiveSec=5s" in lines
    assert "OnUnitActiveSec=30s" in lines
    assert "OnUnitInactiveSec=30s" in lines
    assert "AccuracySec=1s" in lines
    assert "RandomizedDelaySec=0" in lines
    assert f"Unit={spec.recovery_service}" in lines
    assert "WantedBy=timers.target" in lines
    assert "ExecStart=" not in text
    assert spec.gateway_unit not in text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transaction_id", "a" * 63),
        ("transaction_id", "A" * 64),
        ("transaction_id", "../" + "a" * 61),
        ("transaction_id", "a" * 63 + "%"),
        ("transaction_id", "a" * 63 + "$"),
        ("transaction_id", "a" * 63 + " "),
        ("manifest_sha256", "b" * 63),
        ("manifest_sha256", "B" * 64),
        ("manifest_sha256", "b" * 63 + "\n"),
        ("reconciler_sha256", "c" * 63),
        ("reconciler_sha256", "c" * 63 + ";"),
        ("gateway_unit", "hermes-cloud-gateway"),
        ("gateway_unit", "--help.service"),
        ("gateway_unit", "-gateway.service"),
        ("gateway_unit", "../gateway.service"),
        ("gateway_unit", "gateway%N.service"),
        ("gateway_unit", "gateway service.service"),
        ("gateway_unit", "gateway.service\nExecStart=/bin/sh"),
    ],
)
def test_unsafe_identifiers_paths_and_substitutions_are_rejected(
    field: str,
    value: str,
) -> None:
    values: dict[str, object] = {
        "transaction_id": TX,
        "manifest_sha256": MANIFEST,
        "reconciler_sha256": RECONCILER,
    }
    values[field] = value
    with pytest.raises(OfflineRecoverySystemdError):
        OfflineRecoverySystemdSpec(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("first_retry_seconds", 0),
        ("first_retry_seconds", 301),
        ("first_retry_seconds", True),
        ("retry_seconds", 4),
        ("retry_seconds", 3601),
        ("retry_seconds", "30"),
    ],
)
def test_invalid_timer_ranges_are_rejected(field: str, value: object) -> None:
    values: dict[str, object] = {
        "transaction_id": TX,
        "manifest_sha256": MANIFEST,
        "reconciler_sha256": RECONCILER,
    }
    values[field] = value
    with pytest.raises(OfflineRecoverySystemdError):
        OfflineRecoverySystemdSpec(**values)  # type: ignore[arg-type]


def test_only_exact_reconciler_verbs_are_renderable(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    assert spec.reconciler_argv("authorize-start") == (
        "/usr/bin/python3",
        "-I",
        "-S",
        "-B",
        f"/usr/local/libexec/muncho-offline-release-reconcile-{RECONCILER}.py",
        "authorize-start",
        f"--manifest=/var/lib/muncho-offline-release-transactions/{TX}/"
        "transaction-manifest.json",
        f"--manifest-sha256={MANIFEST}",
    )
    assert "reconcile" in spec.reconciler_argv("reconcile")
    for invalid in ("", "recover", "gate", "reconcile; /bin/sh", "RECONCILE"):
        with pytest.raises(OfflineRecoverySystemdError):
            spec.reconciler_argv(invalid)


def test_reconciler_owns_only_exact_nonblocking_gateway_enqueue(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    assert spec.gateway_enqueue_argv() == (
        "/usr/bin/systemctl",
        "start",
        "--no-block",
        "--",
        spec.gateway_unit,
    )
    service = _text(render_recovery_service(spec))
    assert "ExecStartPost=" not in service
    assert "/usr/bin/systemctl" not in service


def test_rendered_artifacts_are_root_owned_regular_unit_inputs(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    artifacts = render_scaffold(spec)
    assert len(artifacts) == 3
    assert len({artifact.path for artifact in artifacts}) == 3
    assert all(artifact.mode == 0o644 for artifact in artifacts)
    assert all(artifact.owner_uid == 0 for artifact in artifacts)
    assert all(artifact.owner_gid == 0 for artifact in artifacts)
    assert all(artifact.content.endswith(b"\n") for artifact in artifacts)
    assert all(b"\r" not in artifact.content for artifact in artifacts)
    validate_rendered_scaffold(spec, artifacts)

    digests = scaffold_sha256s(spec)
    assert set(digests) == {str(artifact.path) for artifact in artifacts}
    assert set(digests.values()) == {artifact.sha256 for artifact in artifacts}
    assert all(len(value) == 64 for value in digests.values())


def test_render_validation_rejects_duplicate_path(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    artifacts = render_scaffold(spec)
    with pytest.raises(OfflineRecoverySystemdError, match="duplicate"):
        validate_rendered_scaffold(spec, (*artifacts, artifacts[0]))


def test_render_validation_rejects_extra_or_missing_path(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    artifacts = render_scaffold(spec)
    extra = UnitArtifact(
        path=PurePosixPath("/etc/systemd/system/unrelated.service"),
        content=b"[Service]\nType=oneshot\n",
    )
    with pytest.raises(OfflineRecoverySystemdError, match="path set"):
        validate_rendered_scaffold(spec, (*artifacts[1:], extra))


@pytest.mark.parametrize("mutation", ["content", "mode", "owner", "group"])
def test_render_validation_rejects_mutated_artifact(
    spec: OfflineRecoverySystemdSpec,
    mutation: str,
) -> None:
    artifacts = list(render_scaffold(spec))
    original = artifacts[0]
    changes: dict[str, object] = {}
    if mutation == "content":
        changes["content"] = original.content + b"# injected\n"
    elif mutation == "mode":
        changes["mode"] = 0o666
    elif mutation == "owner":
        changes["owner_uid"] = 1000
    else:
        changes["owner_gid"] = 1000
    artifacts[0] = replace(original, **changes)

    with pytest.raises(OfflineRecoverySystemdError, match="mismatch"):
        validate_rendered_scaffold(spec, artifacts)


def test_prearm_plan_refuses_unverified_durable_artifacts(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    for value in (False, None, 1, "yes"):
        with pytest.raises(
            OfflineRecoverySystemdError,
            match="durable artifact",
        ):
            prearm_systemctl_commands(  # type: ignore[arg-type]
                spec,
                durable_artifacts_verified=value,
            )


def test_prearm_plan_is_foreground_ordered_and_never_arms_or_starts_oneshot(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    commands = prearm_systemctl_commands(
        spec,
        durable_artifacts_verified=True,
    )
    assert [command.name for command in commands] == [
        "verify_unit_syntax",
        "daemon_reload",
        "enable_recovery_service_and_timer",
        "start_retry_timer_before_arm",
        "verify_recovery_service_enabled",
        "verify_recovery_timer_enabled",
        "verify_recovery_timer_active",
    ]
    assert commands[0].argv[:2] == (
        "/usr/bin/systemd-analyze",
        "verify",
    )
    assert commands[1].argv == ("/usr/bin/systemctl", "daemon-reload")
    assert commands[2].argv == (
        "/usr/bin/systemctl",
        "enable",
        "--",
        spec.recovery_service,
        spec.recovery_timer,
    )
    assert commands[3].argv == (
        "/usr/bin/systemctl",
        "start",
        "--",
        spec.recovery_timer,
    )
    flat = [token for command in commands for token in command.argv]
    assert "--now" not in flat
    assert "arm" not in flat
    assert "/bin/sh" not in flat
    assert "/bin/bash" not in flat
    assert "-c" not in flat
    assert (
        "/usr/bin/systemctl",
        "start",
        "--",
        spec.recovery_service,
    ) not in {command.argv for command in commands}


def test_prearm_readback_commands_cover_every_loaded_relationship(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    commands = prearm_readback_commands(spec)
    assert [command.name for command in commands] == [
        "read_gateway_relationships",
        "read_recovery_service_relationships",
        "read_recovery_timer_relationships",
    ]
    assert commands[0].argv == (
        "/usr/bin/systemctl",
        "show",
        "--property=DropInPaths",
        "--property=Wants",
        "--property=After",
        "--property=ExecConditionEx",
        "--",
        spec.gateway_unit,
    )
    assert commands[1].argv == (
        "/usr/bin/systemctl",
        "show",
        "--property=FragmentPath",
        "--property=Before",
        "--property=DropInPaths",
        "--property=ExecStartEx",
        "--",
        spec.recovery_service,
    )
    assert commands[2].argv == (
        "/usr/bin/systemctl",
        "show",
        "--property=FragmentPath",
        "--property=Triggers",
        "--property=DropInPaths",
        "--",
        spec.recovery_timer,
    )
    assert all("/bin/sh" not in command.argv for command in commands)
    assert all("-c" not in command.argv for command in commands)


def test_loaded_scaffold_accepts_expected_membership_with_unrelated_units(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    validate_loaded_scaffold(spec, _observation(spec))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("recovery_service_enabled", False, "service is not enabled"),
        ("recovery_timer_enabled", False, "timer is not enabled"),
        ("recovery_timer_active", False, "timer is not active"),
        (
            "recovery_service_fragment_path",
            "/tmp/recovery.service",
            "wrong recovery service",
        ),
        (
            "recovery_timer_fragment_path",
            "/tmp/recovery.timer",
            "wrong recovery timer",
        ),
        ("gateway_drop_in_paths", frozenset(), "did not load"),
        ("gateway_wants", frozenset(), "does not want"),
        ("gateway_after", frozenset(), "not ordered after"),
        ("gateway_exec_conditions", (), "exact transaction gate"),
        ("recovery_before", frozenset(), "not ordered before"),
        (
            "recovery_drop_in_paths",
            frozenset({"/etc/systemd/system/recovery.service.d/override.conf"}),
            "unexpected effective drop-ins",
        ),
        ("recovery_exec_starts", (), "exact sealed reconciler"),
        (
            "timer_drop_in_paths",
            frozenset({"/etc/systemd/system/recovery.timer.d/override.conf"}),
            "unexpected effective drop-ins",
        ),
        ("timer_triggers", frozenset(), "does not trigger"),
    ],
)
def test_loaded_scaffold_rejects_each_missing_prearm_fact(
    spec: OfflineRecoverySystemdSpec,
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(OfflineRecoverySystemdError, match=message):
        validate_loaded_scaffold(
            spec,
            replace(_observation(spec), **{field: value}),
        )


@pytest.mark.parametrize(
    "effective",
    [
        (),
        (
            EffectiveCommand(
                argv=("/bin/sh", "-c", "exit 0"),
                fully_privileged=True,
                ignore_failure=False,
            ),
        ),
        (
            EffectiveCommand(
                argv=(
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    "-B",
                    "/usr/local/libexec/other.py",
                    "authorize-start",
                ),
                fully_privileged=True,
                ignore_failure=False,
            ),
        ),
        (
            EffectiveCommand(
                argv=("/usr/bin/true",),
                fully_privileged=False,
                ignore_failure=False,
            ),
            EffectiveCommand(
                argv=("/usr/bin/false",),
                fully_privileged=False,
                ignore_failure=False,
            ),
        ),
    ],
)
def test_prearm_rejects_reset_replaced_or_additional_exec_condition(
    spec: OfflineRecoverySystemdSpec,
    effective: tuple[EffectiveCommand, ...],
) -> None:
    with pytest.raises(OfflineRecoverySystemdError, match="ExecCondition"):
        validate_loaded_scaffold(
            spec,
            replace(_observation(spec), gateway_exec_conditions=effective),
        )


@pytest.mark.parametrize(
    "effective",
    [
        (
            EffectiveCommand(
                argv=(
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    "-B",
                    f"/usr/local/libexec/muncho-offline-release-reconcile-"
                    f"{RECONCILER}.py",
                    "authorize-start",
                    f"--manifest=/var/lib/muncho-offline-release-transactions/"
                    f"{TX}/transaction-manifest.json",
                    f"--manifest-sha256={MANIFEST}",
                ),
                fully_privileged=False,
                ignore_failure=False,
            ),
        ),
        (
            EffectiveCommand(
                argv=(
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    "-B",
                    f"/usr/local/libexec/muncho-offline-release-reconcile-"
                    f"{RECONCILER}.py",
                    "authorize-start",
                    f"--manifest=/var/lib/muncho-offline-release-transactions/"
                    f"{TX}/transaction-manifest.json",
                    f"--manifest-sha256={MANIFEST}",
                ),
                fully_privileged=True,
                ignore_failure=True,
            ),
        ),
    ],
)
def test_prearm_rejects_weakened_exec_condition_flags(
    spec: OfflineRecoverySystemdSpec,
    effective: tuple[EffectiveCommand, ...],
) -> None:
    with pytest.raises(OfflineRecoverySystemdError, match="ExecCondition"):
        validate_loaded_scaffold(
            spec,
            replace(_observation(spec), gateway_exec_conditions=effective),
        )


def test_prearm_rejects_replaced_or_additional_recovery_exec_start(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    wrong = EffectiveCommand(
        argv=("/bin/sh", "-c", "exit 0"),
        fully_privileged=False,
        ignore_failure=False,
    )
    for effective in ((), (wrong,), (spec.expected_recovery_exec_start, wrong)):
        with pytest.raises(OfflineRecoverySystemdError, match="ExecStart"):
            validate_loaded_scaffold(
                spec,
                replace(_observation(spec), recovery_exec_starts=effective),
            )


def test_prearm_rejects_every_other_transaction_namespace_relationship(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    stale = "4" * 64
    stale_service = f"muncho-offline-release-recovery-{stale}.service"
    stale_drop_in = (
        f"/etc/systemd/system/{spec.gateway_unit}.d/"
        f"90-muncho-offline-recovery-{stale}.conf"
    )
    cases = (
        (
            "gateway_drop_in_paths",
            _observation(spec).gateway_drop_in_paths | {stale_drop_in},
            "another transaction recovery drop-in",
        ),
        (
            "gateway_wants",
            _observation(spec).gateway_wants | {stale_service},
            "wants another transaction",
        ),
        (
            "gateway_after",
            _observation(spec).gateway_after | {stale_service},
            "ordered after another transaction",
        ),
        (
            "timer_triggers",
            _observation(spec).timer_triggers | {stale_service},
            "exactly the recovery service",
        ),
    )
    for field, value, message in cases:
        with pytest.raises(OfflineRecoverySystemdError, match=message):
            validate_loaded_scaffold(
                spec,
                replace(_observation(spec), **{field: value}),
            )


def test_cleanup_refuses_before_durable_final_health(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    for value in (False, None, 1, "yes"):
        with pytest.raises(
            OfflineRecoverySystemdError,
            match="durable final health",
        ):
            cleanup_systemctl_commands(  # type: ignore[arg-type]
                spec,
                durable_final_health_verified=value,
            )


def test_cleanup_plan_cannot_stop_its_own_oneshot_and_separates_unlink(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    plan = cleanup_systemctl_commands(
        spec,
        durable_final_health_verified=True,
    )
    assert isinstance(plan, CleanupSystemctlPlan)
    assert [command.name for command in plan.before_unlink] == [
        "stop_retry_timer",
        "disable_recovery_service_and_timer",
    ]
    assert plan.before_unlink[0].argv == (
        "/usr/bin/systemctl",
        "stop",
        "--",
        spec.recovery_timer,
    )
    assert plan.before_unlink[1].argv == (
        "/usr/bin/systemctl",
        "disable",
        "--",
        spec.recovery_timer,
        spec.recovery_service,
    )
    assert [command.name for command in plan.after_unlink] == [
        "daemon_reload_after_unlink"
    ]
    assert plan.after_unlink[0].argv == (
        "/usr/bin/systemctl",
        "daemon-reload",
    )
    all_argv = {command.argv for command in (*plan.before_unlink, *plan.after_unlink)}
    assert (
        "/usr/bin/systemctl",
        "stop",
        "--",
        spec.recovery_service,
    ) not in all_argv
    assert not any("--now" in argv for argv in all_argv)


def _cleaned_observation(
    spec: OfflineRecoverySystemdSpec,
) -> CleanedScaffoldObservation:
    return CleanedScaffoldObservation(
        recovery_service_enabled=False,
        recovery_timer_enabled=False,
        recovery_timer_active=False,
        gateway_drop_in_paths=frozenset({"/etc/systemd/system/other.conf"}),
        gateway_wants=frozenset({"network-online.target"}),
        gateway_after=frozenset({"basic.target"}),
        gateway_exec_conditions=(
            EffectiveCommand(
                argv=("/usr/libexec/unrelated-gate",),
                fully_privileged=False,
                ignore_failure=False,
            ),
        ),
    )


def test_manifest_then_scaffold_construction_has_no_digest_fixed_point() -> None:
    manifest_payload = {
        "schema": "muncho-offline-deploy-manifest-envelope.v1",
        "transaction_id": TX,
        "reconciler_sha256": RECONCILER,
        "scaffolding_contract": "muncho-offline-recovery-systemd.v1",
    }
    manifest_bytes = json.dumps(
        manifest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    built = OfflineRecoverySystemdSpec(
        transaction_id=TX,
        manifest_sha256=manifest_sha256,
        reconciler_sha256=RECONCILER,
    )
    artifacts = render_scaffold(built)
    validate_rendered_scaffold(built, artifacts)

    artifacts_by_path = {artifact.path: artifact for artifact in artifacts}
    assert (
        manifest_sha256.encode("ascii")
        in artifacts_by_path[built.recovery_service_path].content
    )
    assert (
        manifest_sha256.encode("ascii")
        in artifacts_by_path[built.gateway_drop_in_path].content
    )
    attestation = scaffold_sha256s(built)
    assert attestation == {
        str(artifact.path): hashlib.sha256(artifact.content).hexdigest()
        for artifact in artifacts
    }
    assert "scaffold_sha256s" not in manifest_payload
    assert hashlib.sha256(manifest_bytes).hexdigest() == manifest_sha256


def test_cleanup_readback_commands_cover_enablement_activity_and_gateway(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    commands = cleanup_readback_commands(spec)
    assert [command.name for command in commands] == [
        "read_recovery_service_disabled",
        "read_recovery_timer_disabled",
        "read_recovery_timer_inactive",
        "read_gateway_cleanup_relationships",
    ]
    assert commands[0].argv == (
        "/usr/bin/systemctl",
        "is-enabled",
        "--quiet",
        "--",
        spec.recovery_service,
    )
    assert commands[1].argv == (
        "/usr/bin/systemctl",
        "is-enabled",
        "--quiet",
        "--",
        spec.recovery_timer,
    )
    assert commands[2].argv == (
        "/usr/bin/systemctl",
        "is-active",
        "--quiet",
        "--",
        spec.recovery_timer,
    )
    assert commands[3].argv == (
        "/usr/bin/systemctl",
        "show",
        "--property=DropInPaths",
        "--property=Wants",
        "--property=After",
        "--property=ExecConditionEx",
        "--",
        spec.gateway_unit,
    )


def test_cleaned_scaffold_accepts_complete_removal(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    validate_cleaned_scaffold(spec, _cleaned_observation(spec))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("recovery_service_enabled", True, "service remains enabled"),
        ("recovery_timer_enabled", True, "timer remains enabled"),
        ("recovery_timer_active", True, "timer remains active"),
    ],
)
def test_cleaned_scaffold_rejects_retained_boolean_state(
    spec: OfflineRecoverySystemdSpec,
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(OfflineRecoverySystemdError, match=message):
        validate_cleaned_scaffold(
            spec,
            replace(_cleaned_observation(spec), **{field: value}),
        )


def test_cleaned_scaffold_rejects_each_retained_gateway_relationship(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    cases = (
        (
            "gateway_drop_in_paths",
            frozenset({str(spec.gateway_drop_in_path)}),
            "drop-in remains loaded",
        ),
        (
            "gateway_wants",
            frozenset({spec.recovery_service}),
            "still wants transaction recovery",
        ),
        (
            "gateway_after",
            frozenset({spec.recovery_service}),
            "ordered after transaction recovery",
        ),
        (
            "gateway_exec_conditions",
            (spec.expected_gateway_exec_condition,),
            "ExecCondition remains",
        ),
    )
    for field, value, message in cases:
        with pytest.raises(OfflineRecoverySystemdError, match=message):
            validate_cleaned_scaffold(
                spec,
                replace(_cleaned_observation(spec), **{field: value}),
            )


def test_cleanup_rejects_stale_transaction_namespace_not_only_current(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    stale = "5" * 64
    stale_service = f"muncho-offline-release-recovery-{stale}.service"
    stale_drop_in = (
        f"/etc/systemd/system/{spec.gateway_unit}.d/"
        f"90-muncho-offline-recovery-{stale}.conf"
    )
    stale_gate = EffectiveCommand(
        argv=(
            "/usr/bin/python3",
            "-I",
            "-S",
            "-B",
            f"/usr/local/libexec/muncho-offline-release-reconcile-{'6' * 64}.py",
            "authorize-start",
            f"--manifest=/var/lib/muncho-offline-release-transactions/"
            f"{stale}/transaction-manifest.json",
            f"--manifest-sha256={'7' * 64}",
        ),
        fully_privileged=True,
        ignore_failure=False,
    )
    cases = (
        (
            "gateway_drop_in_paths",
            frozenset({stale_drop_in}),
            "drop-in remains loaded",
        ),
        (
            "gateway_wants",
            frozenset({stale_service}),
            "still wants transaction recovery",
        ),
        (
            "gateway_after",
            frozenset({stale_service}),
            "ordered after transaction recovery",
        ),
        (
            "gateway_exec_conditions",
            (stale_gate,),
            "ExecCondition remains",
        ),
    )
    for field, value, message in cases:
        with pytest.raises(OfflineRecoverySystemdError, match=message):
            validate_cleaned_scaffold(
                spec,
                replace(_cleaned_observation(spec), **{field: value}),
            )


def _reserved_namespace(
    spec: OfflineRecoverySystemdSpec,
    *,
    cleaned: bool = False,
) -> ReservedNamespaceObservation:
    empty = frozenset()
    return ReservedNamespaceObservation(
        recovery_service_files=empty
        if cleaned
        else frozenset({str(spec.recovery_service_path)}),
        recovery_timer_files=empty
        if cleaned
        else frozenset({str(spec.recovery_timer_path)}),
        gateway_drop_in_files=empty
        if cleaned
        else frozenset({str(spec.gateway_drop_in_path)}),
        reconciler_files=empty
        if cleaned
        else frozenset({str(spec.reconciler_path)}),
        transaction_state_directories=empty
        if cleaned
        else frozenset({str(spec.state_directory)}),
        enabled_recovery_services=empty
        if cleaned
        else frozenset({spec.recovery_service}),
        enabled_recovery_timers=empty
        if cleaned
        else frozenset({spec.recovery_timer}),
        active_recovery_services=empty,
        active_recovery_timers=empty
        if cleaned
        else frozenset({spec.recovery_timer}),
    )


def test_reserved_namespace_accepts_only_current_transaction(
    spec: OfflineRecoverySystemdSpec,
) -> None:
    validate_reserved_namespace(
        spec,
        _reserved_namespace(spec),
        cleaned=False,
    )
    validate_reserved_namespace(
        spec,
        _reserved_namespace(spec, cleaned=True),
        cleaned=True,
    )


@pytest.mark.parametrize(
    "field,stale",
    (
        (
            "recovery_service_files",
            "/etc/systemd/system/muncho-offline-release-recovery-"
            + "8" * 64
            + ".service",
        ),
        (
            "recovery_timer_files",
            "/etc/systemd/system/muncho-offline-release-recovery-"
            + "8" * 64
            + ".timer",
        ),
        (
            "enabled_recovery_services",
            "muncho-offline-release-recovery-" + "8" * 64 + ".service",
        ),
        (
            "active_recovery_timers",
            "muncho-offline-release-recovery-" + "8" * 64 + ".timer",
        ),
        (
            "transaction_state_directories",
            "/var/lib/muncho-offline-release-transactions/" + "8" * 64,
        ),
    ),
)
def test_reserved_namespace_rejects_orphans_not_linked_to_gateway(
    spec: OfflineRecoverySystemdSpec,
    field: str,
    stale: str,
) -> None:
    observation = _reserved_namespace(spec)
    with pytest.raises(
        OfflineRecoverySystemdError,
        match="reserved recovery namespace mismatch",
    ):
        validate_reserved_namespace(
            spec,
            replace(
                observation,
                **{
                    field: getattr(observation, field)
                    | frozenset({stale}),
                },
            ),
            cleaned=False,
        )


def test_systemd_analyze_accepts_rendered_service_and_timer_when_available(
    spec: OfflineRecoverySystemdSpec,
    tmp_path: Path,
) -> None:
    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is None:
        pytest.skip("systemd-analyze is unavailable on this host")

    service = render_recovery_service(spec)
    timer = render_recovery_timer(spec)
    service_path = tmp_path / spec.recovery_service
    timer_path = tmp_path / spec.recovery_timer
    service_path.write_bytes(service.content)
    timer_path.write_bytes(timer.content)

    result = subprocess.run(
        [systemd_analyze, "verify", str(service_path), str(timer_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
