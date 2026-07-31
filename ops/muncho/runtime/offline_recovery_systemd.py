"""Render the transaction-scoped systemd recovery scaffold for Muncho.

This module deliberately does *not* implement release repair, owner rotation,
state reconciliation, or gateway health checks.  It defines the small,
testable systemd boundary which keeps an already-armed offline release
transaction recoverable across foreground-process death and host reboot.

The state reconciler is a content-addressed, root-owned, standard-library-only
Python file outside every mutable release.  Both systemd entry points invoke
it through ``/usr/bin/python3 -I -S -B`` (isolated mode, no ``site`` or
ambient ``.pth`` loading, no bytecode writes) and bind the exact transaction-
manifest bytes by SHA-256:

* the gateway ``ExecCondition`` asks the reconciler whether this exact gateway
  start is permitted;
* the recovery oneshot asks it to converge the exact transaction and, only
  after a permitted terminal selection is durable and its shared transaction
  lock is released, the reconciler itself enqueues the gateway with the exact
  argv returned by :meth:`OfflineRecoverySystemdSpec.gateway_enqueue_argv`.
  A unit-level ``ExecStartPost`` is intentionally not used because it would
  run after failed or incomplete decisions and can form an ordering cycle.

The reconciler owns that state-machine decision.  Its required contract is:

* no arm marker: gate allows and recovery performs no mutation;
* armed, no canonical activation boundary, exact predecessor triplet:
  recovery may only abort to the unchanged predecessor;
* canonical activation boundary, final receipt, or any live triplet drift:
  recovery may only finalize the prebuilt successor;
* unknown or mixed evidence: retain the gate and drain, and fail closed;
* after arm, start authorization and state/link replay need no clock, network,
  IAP, package reconstruction, or mutable release code; a later health pass
  may exercise already-configured production dependencies, but its failure
  retains the gate and retry timer;
* the transaction lock and held-drain capability are acquired and checked by
  the reconciler itself before it changes state.

Lifecycle ordering is intentionally split around the irreversible arm write:

1. Durably publish the exact manifest, content-addressed reconciler, unit
   files, and gateway drop-in.  Fsync the files and containing directories.
2. Run :func:`prearm_systemctl_commands` in order, then capture and validate
   :class:`LoadedScaffoldObservation`.
3. Write and fsync the arm marker *last*.  The retry timer is already active;
   a pre-arm recovery invocation is a required no-op.
4. Keep the scaffold armed until a digest-bound, live-identity-verified final
   health receipt is durable.
5. Only then run :func:`cleanup_systemctl_commands`, unlink the three rendered
   artifacts, fsync their directories, daemon-reload, and verify removal.

If cleanup is interrupted, the durable health/cleanup state must make the gate
permit only the exact successor and make recovery finish cleanup idempotently.
The scaffold is temporary transaction state, not permanent generic recovery
infrastructure.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Mapping


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*\.service\Z")

SYSTEMD_ROOT = PurePosixPath("/etc/systemd/system")
STATE_ROOT = PurePosixPath("/var/lib/muncho-offline-release-transactions")
LIBEXEC_ROOT = PurePosixPath("/usr/local/libexec")
SYSTEM_PYTHON = PurePosixPath("/usr/bin/python3")
SYSTEMCTL = PurePosixPath("/usr/bin/systemctl")
SYSTEMD_ANALYZE = PurePosixPath("/usr/bin/systemd-analyze")
DEFAULT_GATEWAY_UNIT = "hermes-cloud-gateway.service"
DEFAULT_ACTIVE_LINK = PurePosixPath("/opt/adventico-ai-platform/hermes-agent")
RECOVERY_UNIT_PREFIX = "muncho-offline-release-recovery-"
GATEWAY_DROP_IN_PREFIX = "90-muncho-offline-recovery-"
RECONCILER_PATH_PREFIX = "/usr/local/libexec/muncho-offline-release-reconcile-"
MANIFEST_ARGUMENT_PREFIX = "--manifest=/var/lib/muncho-offline-release-transactions/"


class OfflineRecoverySystemdError(ValueError):
    """The rendered or observed scaffold violates the fail-closed contract."""


def _require_sha256(name: str, value: str) -> None:
    if _SHA256_RE.fullmatch(value) is None:
        raise OfflineRecoverySystemdError(
            f"{name} must be exactly 64 lowercase hexadecimal characters"
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unit_bytes(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def _is_transaction_recovery_service(value: str) -> bool:
    return value.startswith(RECOVERY_UNIT_PREFIX) and value.endswith(".service")


def _is_transaction_gateway_drop_in(value: str) -> bool:
    return PurePosixPath(value).name.startswith(GATEWAY_DROP_IN_PREFIX)


def _is_transaction_gate_command(value: EffectiveCommand) -> bool:
    return any(
        argument.startswith(RECONCILER_PATH_PREFIX)
        or argument.startswith(MANIFEST_ARGUMENT_PREFIX)
        for argument in value.argv
    )


@dataclass(frozen=True)
class UnitArtifact:
    """One exact root-owned systemd artifact."""

    path: PurePosixPath
    content: bytes
    mode: int = 0o644
    owner_uid: int = 0
    owner_gid: int = 0

    @property
    def sha256(self) -> str:
        return _sha256(self.content)


@dataclass(frozen=True)
class NamedCommand:
    """One argv-only lifecycle command; no shell interpolation is permitted."""

    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class EffectiveCommand:
    """One mechanically decoded command from an effective systemd property."""

    argv: tuple[str, ...]
    fully_privileged: bool
    ignore_failure: bool


@dataclass(frozen=True)
class CleanupSystemctlPlan:
    """Commands separated by the required durable artifact-unlink boundary."""

    before_unlink: tuple[NamedCommand, ...]
    after_unlink: tuple[NamedCommand, ...]


@dataclass(frozen=True)
class LoadedScaffoldObservation:
    """Read-only facts captured after the pre-arm systemctl commands."""

    recovery_service_enabled: bool
    recovery_timer_enabled: bool
    recovery_timer_active: bool
    recovery_service_fragment_path: str
    recovery_timer_fragment_path: str
    gateway_drop_in_paths: frozenset[str]
    gateway_wants: frozenset[str]
    gateway_after: frozenset[str]
    gateway_exec_conditions: tuple[EffectiveCommand, ...]
    recovery_before: frozenset[str]
    recovery_drop_in_paths: frozenset[str]
    recovery_exec_starts: tuple[EffectiveCommand, ...]
    timer_drop_in_paths: frozenset[str]
    timer_triggers: frozenset[str]


@dataclass(frozen=True)
class CleanedScaffoldObservation:
    """Read-only facts captured after terminal scaffold cleanup."""

    recovery_service_enabled: bool
    recovery_timer_enabled: bool
    recovery_timer_active: bool
    gateway_drop_in_paths: frozenset[str]
    gateway_wants: frozenset[str]
    gateway_after: frozenset[str]
    gateway_exec_conditions: tuple[EffectiveCommand, ...]


@dataclass(frozen=True)
class ReservedNamespaceObservation:
    """Closed-world inventory of every offline-release recovery namespace."""

    recovery_service_files: frozenset[str]
    recovery_timer_files: frozenset[str]
    gateway_drop_in_files: frozenset[str]
    reconciler_files: frozenset[str]
    transaction_state_directories: frozenset[str]
    enabled_recovery_services: frozenset[str]
    enabled_recovery_timers: frozenset[str]
    active_recovery_services: frozenset[str]
    active_recovery_timers: frozenset[str]


@dataclass(frozen=True)
class OfflineRecoverySystemdSpec:
    """Content-addressed names and paths for one offline transaction."""

    transaction_id: str
    manifest_sha256: str
    reconciler_sha256: str
    gateway_unit: str = DEFAULT_GATEWAY_UNIT
    first_retry_seconds: int = 5
    retry_seconds: int = 30

    def __post_init__(self) -> None:
        _require_sha256("transaction_id", self.transaction_id)
        _require_sha256("manifest_sha256", self.manifest_sha256)
        _require_sha256("reconciler_sha256", self.reconciler_sha256)
        if _UNIT_RE.fullmatch(self.gateway_unit) is None:
            raise OfflineRecoverySystemdError(
                "gateway_unit must be one safe systemd .service unit name"
            )
        if type(self.first_retry_seconds) is not int or not (
            1 <= self.first_retry_seconds <= 300
        ):
            raise OfflineRecoverySystemdError(
                "first_retry_seconds must be an integer from 1 through 300"
            )
        if type(self.retry_seconds) is not int or not (5 <= self.retry_seconds <= 3600):
            raise OfflineRecoverySystemdError(
                "retry_seconds must be an integer from 5 through 3600"
            )

    @property
    def state_directory(self) -> PurePosixPath:
        return STATE_ROOT / self.transaction_id

    @property
    def manifest_path(self) -> PurePosixPath:
        return self.state_directory / "transaction-manifest.json"

    @property
    def reconciler_path(self) -> PurePosixPath:
        return LIBEXEC_ROOT / (
            f"muncho-offline-release-reconcile-{self.reconciler_sha256}.py"
        )

    @property
    def recovery_stem(self) -> str:
        return f"muncho-offline-release-recovery-{self.transaction_id}"

    @property
    def recovery_service(self) -> str:
        return f"{self.recovery_stem}.service"

    @property
    def recovery_timer(self) -> str:
        return f"{self.recovery_stem}.timer"

    @property
    def recovery_service_path(self) -> PurePosixPath:
        return SYSTEMD_ROOT / self.recovery_service

    @property
    def recovery_timer_path(self) -> PurePosixPath:
        return SYSTEMD_ROOT / self.recovery_timer

    @property
    def gateway_fragment_path(self) -> PurePosixPath:
        return SYSTEMD_ROOT / self.gateway_unit

    @property
    def gateway_drop_in_path(self) -> PurePosixPath:
        return (
            SYSTEMD_ROOT
            / f"{self.gateway_unit}.d"
            / f"90-muncho-offline-recovery-{self.transaction_id}.conf"
        )

    def reconciler_argv(self, verb: str) -> tuple[str, ...]:
        if verb not in {"authorize-start", "reconcile"}:
            raise OfflineRecoverySystemdError(
                "reconciler verb must be exactly 'authorize-start' or 'reconcile'"
            )
        return (
            str(SYSTEM_PYTHON),
            "-I",
            "-S",
            "-B",
            str(self.reconciler_path),
            verb,
            f"--manifest={self.manifest_path}",
            f"--manifest-sha256={self.manifest_sha256}",
        )

    def systemd_command(self, verb: str, *, privileged_prefix: bool) -> str:
        argv = self.reconciler_argv(verb)
        executable = f"+{argv[0]}" if privileged_prefix else argv[0]
        # All caller-controlled components are lowercase hex or a validated
        # unit name.  Fixed paths contain no whitespace or systemd metachar.
        return " ".join((executable, *argv[1:]))

    def gateway_enqueue_argv(self) -> tuple[str, ...]:
        """Exact nonblocking argv the reconciler uses after releasing locks."""

        return (
            str(SYSTEMCTL),
            "start",
            "--no-block",
            "--",
            self.gateway_unit,
        )

    @property
    def expected_gateway_exec_condition(self) -> EffectiveCommand:
        return EffectiveCommand(
            argv=self.reconciler_argv("authorize-start"),
            fully_privileged=True,
            ignore_failure=False,
        )

    @property
    def expected_recovery_exec_start(self) -> EffectiveCommand:
        return EffectiveCommand(
            argv=self.reconciler_argv("reconcile"),
            fully_privileged=False,
            ignore_failure=False,
        )


def render_gateway_drop_in(spec: OfflineRecoverySystemdSpec) -> UnitArtifact:
    """Render the start gate and boot-time recovery dependency."""

    return UnitArtifact(
        path=spec.gateway_drop_in_path,
        content=_unit_bytes(
            "# Managed by one armed Muncho offline release transaction.",
            "# Removing this before durable final health is forbidden.",
            "[Unit]",
            f"Wants={spec.recovery_service}",
            f"After={spec.recovery_service}",
            "",
            "[Service]",
            "ExecCondition="
            + spec.systemd_command("authorize-start", privileged_prefix=True),
        ),
    )


def render_recovery_service(spec: OfflineRecoverySystemdSpec) -> UnitArtifact:
    """Render the boot-safe, non-networked recovery oneshot."""

    return UnitArtifact(
        path=spec.recovery_service_path,
        content=_unit_bytes(
            "[Unit]",
            "Description=Recover one armed Muncho offline release transaction",
            "DefaultDependencies=no",
            "Requires=local-fs.target",
            "After=local-fs.target",
            f"Before={spec.gateway_unit} shutdown.target",
            "Conflicts=shutdown.target",
            "StartLimitIntervalSec=0",
            "RequiresMountsFor="
            f"{spec.state_directory} {spec.reconciler_path} "
            f"{DEFAULT_ACTIVE_LINK}",
            "",
            "[Service]",
            "Type=oneshot",
            "User=root",
            "Group=root",
            "UMask=0077",
            "ExecStart=" + spec.systemd_command("reconcile", privileged_prefix=False),
            "TimeoutStartSec=240s",
            "TimeoutStopSec=30s",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ),
    )


def render_recovery_timer(spec: OfflineRecoverySystemdSpec) -> UnitArtifact:
    """Render the retry timer; the enabled service covers the first boot run."""

    return UnitArtifact(
        path=spec.recovery_timer_path,
        content=_unit_bytes(
            "[Unit]",
            "Description=Retry one armed Muncho offline release recovery",
            "After=local-fs.target",
            "",
            "[Timer]",
            f"OnActiveSec={spec.first_retry_seconds}s",
            # OnUnitActiveSec is relative to every attempted activation, so a
            # oneshot which ends in the failed state is retried as well.
            f"OnUnitActiveSec={spec.retry_seconds}s",
            # OnUnitInactiveSec also schedules another pass after a successful
            # incomplete run (for example: select F, enqueue it, then inspect
            # live health on the following pass).
            f"OnUnitInactiveSec={spec.retry_seconds}s",
            "AccuracySec=1s",
            "RandomizedDelaySec=0",
            f"Unit={spec.recovery_service}",
            "",
            "[Install]",
            "WantedBy=timers.target",
        ),
    )


def render_scaffold(
    spec: OfflineRecoverySystemdSpec,
) -> tuple[UnitArtifact, UnitArtifact, UnitArtifact]:
    """Return the exact service, timer, and gateway drop-in artifacts."""

    return (
        render_recovery_service(spec),
        render_recovery_timer(spec),
        render_gateway_drop_in(spec),
    )


def validate_rendered_scaffold(
    spec: OfflineRecoverySystemdSpec,
    artifacts: Iterable[UnitArtifact],
) -> None:
    """Prove an artifact set is byte-for-byte the expected scaffold."""

    expected = {artifact.path: artifact for artifact in render_scaffold(spec)}
    observed: dict[PurePosixPath, UnitArtifact] = {}
    for artifact in artifacts:
        if artifact.path in observed:
            raise OfflineRecoverySystemdError(
                f"duplicate scaffold artifact: {artifact.path}"
            )
        observed[artifact.path] = artifact
    if observed.keys() != expected.keys():
        missing = sorted(str(path) for path in expected.keys() - observed.keys())
        extra = sorted(str(path) for path in observed.keys() - expected.keys())
        raise OfflineRecoverySystemdError(
            f"scaffold path set mismatch: missing={missing!r} extra={extra!r}"
        )
    for path, wanted in expected.items():
        actual = observed[path]
        if actual.content != wanted.content:
            raise OfflineRecoverySystemdError(f"scaffold content mismatch: {path}")
        if (
            actual.mode,
            actual.owner_uid,
            actual.owner_gid,
        ) != (
            wanted.mode,
            wanted.owner_uid,
            wanted.owner_gid,
        ):
            raise OfflineRecoverySystemdError(
                f"scaffold ownership or mode mismatch: {path}"
            )


def scaffold_sha256s(
    spec: OfflineRecoverySystemdSpec,
) -> Mapping[str, str]:
    """Return path-to-digest bindings for the post-manifest arm attestation.

    The rendered files embed ``manifest_sha256`` and therefore their hashes
    must never be included in that manifest's own digest.  They are sealed in
    the separate pre-arm scaffold-attestation receipt instead.
    """

    return {str(artifact.path): artifact.sha256 for artifact in render_scaffold(spec)}


def prearm_systemctl_commands(
    spec: OfflineRecoverySystemdSpec,
    *,
    durable_artifacts_verified: bool,
) -> tuple[NamedCommand, ...]:
    """Return the exact pre-arm command order.

    ``durable_artifacts_verified`` must mean the reconciler, manifest, units,
    drop-in, and all parent directories were independently digest-checked and
    fsynced.  These commands intentionally do not publish the arm marker and do
    not start the recovery service directly.
    """

    if durable_artifacts_verified is not True:
        raise OfflineRecoverySystemdError(
            "refusing pre-arm commands before durable artifact verification"
        )
    return (
        NamedCommand(
            "verify_unit_syntax",
            (
                str(SYSTEMD_ANALYZE),
                "verify",
                "--",
                str(spec.recovery_service_path),
                str(spec.recovery_timer_path),
                str(spec.gateway_fragment_path),
            ),
        ),
        NamedCommand(
            "daemon_reload",
            (str(SYSTEMCTL), "daemon-reload"),
        ),
        NamedCommand(
            "enable_recovery_service_and_timer",
            (
                str(SYSTEMCTL),
                "enable",
                "--",
                spec.recovery_service,
                spec.recovery_timer,
            ),
        ),
        NamedCommand(
            "start_retry_timer_before_arm",
            (str(SYSTEMCTL), "start", "--", spec.recovery_timer),
        ),
        NamedCommand(
            "verify_recovery_service_enabled",
            (
                str(SYSTEMCTL),
                "is-enabled",
                "--quiet",
                "--",
                spec.recovery_service,
            ),
        ),
        NamedCommand(
            "verify_recovery_timer_enabled",
            (
                str(SYSTEMCTL),
                "is-enabled",
                "--quiet",
                "--",
                spec.recovery_timer,
            ),
        ),
        NamedCommand(
            "verify_recovery_timer_active",
            (
                str(SYSTEMCTL),
                "is-active",
                "--quiet",
                "--",
                spec.recovery_timer,
            ),
        ),
    )


def prearm_readback_commands(
    spec: OfflineRecoverySystemdSpec,
) -> tuple[NamedCommand, ...]:
    """Return exact read-only commands used to build the loaded observation."""

    return (
        NamedCommand(
            "read_gateway_relationships",
            (
                str(SYSTEMCTL),
                "show",
                "--property=DropInPaths",
                "--property=Wants",
                "--property=After",
                "--property=ExecConditionEx",
                "--",
                spec.gateway_unit,
            ),
        ),
        NamedCommand(
            "read_recovery_service_relationships",
            (
                str(SYSTEMCTL),
                "show",
                "--property=FragmentPath",
                "--property=Before",
                "--property=DropInPaths",
                "--property=ExecStartEx",
                "--",
                spec.recovery_service,
            ),
        ),
        NamedCommand(
            "read_recovery_timer_relationships",
            (
                str(SYSTEMCTL),
                "show",
                "--property=FragmentPath",
                "--property=Triggers",
                "--property=DropInPaths",
                "--",
                spec.recovery_timer,
            ),
        ),
    )


def validate_loaded_scaffold(
    spec: OfflineRecoverySystemdSpec,
    observation: LoadedScaffoldObservation,
) -> None:
    """Validate loaded unit relationships immediately before arming."""

    if observation.recovery_service_enabled is not True:
        raise OfflineRecoverySystemdError("recovery service is not enabled")
    if observation.recovery_timer_enabled is not True:
        raise OfflineRecoverySystemdError("recovery timer is not enabled")
    if observation.recovery_timer_active is not True:
        raise OfflineRecoverySystemdError("recovery timer is not active")
    if observation.recovery_service_fragment_path != str(spec.recovery_service_path):
        raise OfflineRecoverySystemdError(
            "systemd loaded the wrong recovery service fragment"
        )
    if observation.recovery_timer_fragment_path != str(spec.recovery_timer_path):
        raise OfflineRecoverySystemdError(
            "systemd loaded the wrong recovery timer fragment"
        )
    if str(spec.gateway_drop_in_path) not in observation.gateway_drop_in_paths:
        raise OfflineRecoverySystemdError(
            "gateway did not load the transaction drop-in"
        )
    stale_gateway_drop_ins = sorted(
        path
        for path in observation.gateway_drop_in_paths
        if _is_transaction_gateway_drop_in(path)
        and path != str(spec.gateway_drop_in_path)
    )
    if stale_gateway_drop_ins:
        raise OfflineRecoverySystemdError(
            "gateway loaded another transaction recovery drop-in: "
            f"{stale_gateway_drop_ins!r}"
        )
    if observation.gateway_exec_conditions != (spec.expected_gateway_exec_condition,):
        raise OfflineRecoverySystemdError(
            "effective gateway ExecCondition is not the exact transaction gate"
        )
    if spec.recovery_service not in observation.gateway_wants:
        raise OfflineRecoverySystemdError("gateway does not want the recovery service")
    stale_gateway_wants = sorted(
        unit
        for unit in observation.gateway_wants
        if _is_transaction_recovery_service(unit) and unit != spec.recovery_service
    )
    if stale_gateway_wants:
        raise OfflineRecoverySystemdError(
            "gateway wants another transaction recovery service: "
            f"{stale_gateway_wants!r}"
        )
    if spec.recovery_service not in observation.gateway_after:
        raise OfflineRecoverySystemdError(
            "gateway is not ordered after the recovery service"
        )
    stale_gateway_after = sorted(
        unit
        for unit in observation.gateway_after
        if _is_transaction_recovery_service(unit) and unit != spec.recovery_service
    )
    if stale_gateway_after:
        raise OfflineRecoverySystemdError(
            "gateway is ordered after another transaction recovery service: "
            f"{stale_gateway_after!r}"
        )
    if spec.gateway_unit not in observation.recovery_before:
        raise OfflineRecoverySystemdError(
            "recovery service is not ordered before the gateway"
        )
    if observation.recovery_drop_in_paths:
        raise OfflineRecoverySystemdError(
            "recovery service has unexpected effective drop-ins"
        )
    if observation.recovery_exec_starts != (spec.expected_recovery_exec_start,):
        raise OfflineRecoverySystemdError(
            "effective recovery ExecStart is not the exact sealed reconciler"
        )
    if observation.timer_drop_in_paths:
        raise OfflineRecoverySystemdError(
            "recovery timer has unexpected effective drop-ins"
        )
    if observation.timer_triggers != frozenset({spec.recovery_service}):
        raise OfflineRecoverySystemdError(
            "recovery timer does not trigger exactly the recovery service"
        )


def cleanup_systemctl_commands(
    spec: OfflineRecoverySystemdSpec,
    *,
    durable_final_health_verified: bool,
) -> CleanupSystemctlPlan:
    """Return the safe systemctl portion of terminal scaffold cleanup.

    The caller must first publish and fsync exact final health, then run these
    ``before_unlink`` commands through ``disable``.  It must then unlink the
    three rendered artifacts and fsync both systemd directories before it runs
    ``after_unlink``.  Finally it verifies that no loaded drop-in or enablement
    remains.

    The recovery service is deliberately never stopped here.  This sequence is
    safe when called by that oneshot itself and therefore cannot self-deadlock.
    """

    if durable_final_health_verified is not True:
        raise OfflineRecoverySystemdError(
            "refusing scaffold cleanup before durable final health"
        )
    return CleanupSystemctlPlan(
        before_unlink=(
            NamedCommand(
                "stop_retry_timer",
                (str(SYSTEMCTL), "stop", "--", spec.recovery_timer),
            ),
            NamedCommand(
                "disable_recovery_service_and_timer",
                (
                    str(SYSTEMCTL),
                    "disable",
                    "--",
                    spec.recovery_timer,
                    spec.recovery_service,
                ),
            ),
        ),
        after_unlink=(
            NamedCommand(
                "daemon_reload_after_unlink",
                (str(SYSTEMCTL), "daemon-reload"),
            ),
        ),
    )


def cleanup_readback_commands(
    spec: OfflineRecoverySystemdSpec,
) -> tuple[NamedCommand, ...]:
    """Return exact read-only commands for post-cleanup verification.

    The three ``is-*`` commands are expected to return non-zero after successful
    cleanup.  The caller converts their exit statuses into
    :class:`CleanedScaffoldObservation` rather than treating non-zero as a
    command-runner failure.
    """

    return (
        NamedCommand(
            "read_recovery_service_disabled",
            (
                str(SYSTEMCTL),
                "is-enabled",
                "--quiet",
                "--",
                spec.recovery_service,
            ),
        ),
        NamedCommand(
            "read_recovery_timer_disabled",
            (
                str(SYSTEMCTL),
                "is-enabled",
                "--quiet",
                "--",
                spec.recovery_timer,
            ),
        ),
        NamedCommand(
            "read_recovery_timer_inactive",
            (
                str(SYSTEMCTL),
                "is-active",
                "--quiet",
                "--",
                spec.recovery_timer,
            ),
        ),
        NamedCommand(
            "read_gateway_cleanup_relationships",
            (
                str(SYSTEMCTL),
                "show",
                "--property=DropInPaths",
                "--property=Wants",
                "--property=After",
                "--property=ExecConditionEx",
                "--",
                spec.gateway_unit,
            ),
        ),
    )


def validate_cleaned_scaffold(
    spec: OfflineRecoverySystemdSpec,
    observation: CleanedScaffoldObservation,
) -> None:
    """Prove enablement and loaded gateway relationships were removed."""

    if observation.recovery_service_enabled is not False:
        raise OfflineRecoverySystemdError(
            "recovery service remains enabled after cleanup"
        )
    if observation.recovery_timer_enabled is not False:
        raise OfflineRecoverySystemdError(
            "recovery timer remains enabled after cleanup"
        )
    if observation.recovery_timer_active is not False:
        raise OfflineRecoverySystemdError("recovery timer remains active after cleanup")
    retained_gateway_drop_ins = sorted(
        path
        for path in observation.gateway_drop_in_paths
        if _is_transaction_gateway_drop_in(path)
    )
    if retained_gateway_drop_ins:
        raise OfflineRecoverySystemdError(
            "gateway transaction recovery drop-in remains loaded after cleanup: "
            f"{retained_gateway_drop_ins!r}"
        )
    retained_gateway_wants = sorted(
        unit
        for unit in observation.gateway_wants
        if _is_transaction_recovery_service(unit)
    )
    if retained_gateway_wants:
        raise OfflineRecoverySystemdError(
            "gateway still wants transaction recovery after cleanup: "
            f"{retained_gateway_wants!r}"
        )
    retained_gateway_after = sorted(
        unit
        for unit in observation.gateway_after
        if _is_transaction_recovery_service(unit)
    )
    if retained_gateway_after:
        raise OfflineRecoverySystemdError(
            "gateway remains ordered after transaction recovery after cleanup: "
            f"{retained_gateway_after!r}"
        )
    retained_gateway_conditions = tuple(
        command
        for command in observation.gateway_exec_conditions
        if _is_transaction_gate_command(command)
    )
    if retained_gateway_conditions:
        raise OfflineRecoverySystemdError(
            "gateway transaction ExecCondition remains after cleanup"
        )


def validate_reserved_namespace(
    spec: OfflineRecoverySystemdSpec,
    observation: ReservedNamespaceObservation,
    *,
    cleaned: bool,
) -> None:
    """Reject every stale transaction, including enabled orphan units.

    Gateway relationship checks alone are insufficient: an old enabled timer
    or service can still run at boot without appearing in the current
    gateway's Wants/After properties.  The caller inventories the fixed
    filesystem and systemd namespaces; this validator applies only exact,
    mechanical set equality.
    """

    expected = {
        "recovery_service_files": frozenset()
        if cleaned
        else frozenset({str(spec.recovery_service_path)}),
        "recovery_timer_files": frozenset()
        if cleaned
        else frozenset({str(spec.recovery_timer_path)}),
        "gateway_drop_in_files": frozenset()
        if cleaned
        else frozenset({str(spec.gateway_drop_in_path)}),
        "reconciler_files": frozenset()
        if cleaned
        else frozenset({str(spec.reconciler_path)}),
        "transaction_state_directories": frozenset()
        if cleaned
        else frozenset({str(spec.state_directory)}),
        "enabled_recovery_services": frozenset()
        if cleaned
        else frozenset({spec.recovery_service}),
        "enabled_recovery_timers": frozenset()
        if cleaned
        else frozenset({spec.recovery_timer}),
        # The oneshot may be inactive or be the caller currently validating
        # itself.  No other transaction service is ever permitted.
        "active_recovery_services": frozenset(),
        "active_recovery_timers": frozenset()
        if cleaned
        else frozenset({spec.recovery_timer}),
    }
    observed = {
        name: getattr(observation, name)
        for name in expected
    }
    if observed["active_recovery_services"] not in {
        frozenset(),
        frozenset({spec.recovery_service}),
    }:
        raise OfflineRecoverySystemdError(
            "stale active recovery service remains in reserved namespace"
        )
    observed["active_recovery_services"] = frozenset()
    mismatches = {
        name: {
            "expected": sorted(wanted),
            "observed": sorted(observed[name]),
        }
        for name, wanted in expected.items()
        if observed[name] != wanted
    }
    if mismatches:
        raise OfflineRecoverySystemdError(
            f"reserved recovery namespace mismatch: {mismatches!r}"
        )


__all__ = [
    "DEFAULT_GATEWAY_UNIT",
    "CleanedScaffoldObservation",
    "CleanupSystemctlPlan",
    "EffectiveCommand",
    "LoadedScaffoldObservation",
    "NamedCommand",
    "OfflineRecoverySystemdError",
    "OfflineRecoverySystemdSpec",
    "ReservedNamespaceObservation",
    "UnitArtifact",
    "cleanup_systemctl_commands",
    "cleanup_readback_commands",
    "prearm_readback_commands",
    "prearm_systemctl_commands",
    "render_gateway_drop_in",
    "render_recovery_service",
    "render_recovery_timer",
    "render_scaffold",
    "scaffold_sha256s",
    "validate_cleaned_scaffold",
    "validate_loaded_scaffold",
    "validate_reserved_namespace",
    "validate_rendered_scaffold",
]
