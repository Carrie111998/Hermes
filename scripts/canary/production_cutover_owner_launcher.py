#!/usr/bin/env python3
"""Owner-side authoring for the production Canonical Brain cutover.

This module contains no task semantics.  It turns one fresh, root-collected
public observation receipt into the exact FreezePlan, signs the single
pre-stop authority with an owner-local Ed25519 key, derives the CutoverPlan
from the root-captured final tail, and emits only typed public publications.
The private key is never transported, copied, printed, or staged.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gateway import canonical_writer_production_cutover as cutover
from gateway import production_cron_continuity_package as cron_continuity
from gateway import production_cron_migration as cron_migration
from gateway import production_owner_runtime
from ops.muncho.runtime import mechanical_job_rail, trusted_cron_collector_rail
from scripts.canary import full_canary_owner_launcher as canary_transport
from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_host_authority as host_authority
from scripts.canary import production_database_recovery_gate as database_recovery
from scripts.canary import production_initial_release_bootstrap as initial_bootstrap
from scripts.canary import production_cutover_passkey as cutover_passkey
from scripts.canary import (
    production_cutover_unit_input_successor as unit_successor,
)
from scripts.canary import production_cutover_unit_input_rotation as unit_rotation
from scripts.canary.production_cutover_public_stager import (
    PublicStagingError,
    build_publication,
)


COLLECTOR_SCHEMA = "muncho-production-cutover-authority-inputs.v1"
INITIAL_COLLECTOR_SCHEMA = "muncho-production-cutover-initial-observations.v1"
STOPPED_COLLECTOR_SCHEMA = "muncho-production-cutover-stopped-services.v1"
OWNER_WORKSPACE_SCHEMA = "muncho-production-cutover-owner-workspace.v1"
WORKFLOW_RECEIPT_SCHEMA = "muncho-production-cutover-owner-workflow.v4"
PREPARED_WORKSPACE_SCHEMA = (
    "muncho-production-cutover-passkey-workspace.v4"
)
BRIDGE_BOOTSTRAP_INPUT_SCHEMA = (
    "muncho-production-cutover-bridge-bootstrap-input.v2"
)
BRIDGE_REQUEST_SCHEMA = (
    "muncho-owner-gate-caddy-approval-bridge-request.v2"
)
BRIDGE_RECEIPT_SCHEMA = "muncho-owner-gate-caddy-approval-bridge.v2"
CRON_STAGE_NOOP_SCHEMA = "muncho-production-cron-continuity-stage-noop.v1"
CONVERGENCE_SCHEMA = "muncho-owner-gate-production-convergence.v1"
MAX_JSON = 16 * 1024 * 1024
LEGACY_F5_PREDECESSOR_REVISION = (
    "f5ece3598efba6635e661aaa509d783fa2d802d8"
)
MAX_COLLECTOR_AGE_SECONDS = 900
MINIMUM_V2_APPROVAL_MARGIN_SECONDS = 30
LOCAL_RELEASE_PHASE_COMMANDS = {
    "prepare-release-unit-inputs": "prepare-release-unit-inputs",
    "preauthorize-release-unit-inputs": (
        "preauthorize-release-unit-inputs"
    ),
    "seal-finalize-release-unit-inputs-request": (
        "finalize-release-unit-inputs"
    ),
    "seal-abort-release-unit-inputs-request": (
        "abort-release-unit-inputs"
    ),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CUTOVER_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
_CUTOVER_PASSKEY_REQUEST_FIELDS = frozenset({
    "request_id",
    "action_envelope_sha256",
    "challenge_record_sha256",
    "expires_at_unix",
    "release_sha",
    "plan_sha256",
    "freeze_publication_sha256",
    "action_payload_sha256",
    "transaction_id",
    "approval_url",
    "passkey_only",
    "single_use",
    "control_plane_mutation_performed",
    "source_data_mutation_performed",
    "production_host_mutation_performed",
})
_BRIDGE_BOOTSTRAP_INPUT_FIELDS = frozenset({
    "schema",
    "release_revision",
    "freeze_plan_sha256",
    "freeze_approval_sha256",
    "freeze_publication_sha256",
    "v2_request_id",
    "v2_expires_at_unix",
    "v2_transaction_id",
    "v2_approval_url_sha256",
    "v2_action_payload_sha256",
    "document_sha256",
})
_BRIDGE_REQUEST_FIELDS = frozenset({
    "schema",
    "release_revision",
    "freeze_plan_sha256",
    "freeze_approval_sha256",
    "freeze_publication_sha256",
    "v2_request_id",
    "v2_expires_at_unix",
    "v2_transaction_id",
    "v2_approval_url_sha256",
    "v2_action_payload_sha256",
    "bootstrap_input_sha256",
    "legacy_passkey_request_id",
    "legacy_passkey_request_sha256",
    "legacy_approval_url",
    "bridge_action_sha256",
    "route_contract_sha256",
    "original_caddy_sha256",
    "approval_bridge_template_sha256",
    "approval_bridge_caddy_sha256",
    "default_local_v1_route_preserved",
    "control_plane_mutation_performed",
    "source_data_mutation_performed",
    "production_host_mutation_performed",
    "caller_selected_input_accepted",
    "secret_material_recorded",
    "secret_digest_recorded",
    "requested_at_unix",
    "receipt_sha256",
})
_BRIDGE_RECEIPT_FIELDS = frozenset({
    "schema",
    "release_revision",
    "freeze_plan_sha256",
    "freeze_approval_sha256",
    "freeze_publication_sha256",
    "v2_request_id",
    "v2_expires_at_unix",
    "v2_transaction_id",
    "v2_approval_url_sha256",
    "v2_action_payload_sha256",
    "bootstrap_input_sha256",
    "bridge_request_receipt_sha256",
    "legacy_passkey_request_id",
    "legacy_passkey_request_sha256",
    "legacy_passkey_grant_id",
    "legacy_passkey_grant_sha256",
    "legacy_passkey_consumed_grant_sha256",
    "legacy_passkey_consume_entry_sha256",
    "legacy_service_active_before_sha256",
    "legacy_service_inactive_sha256",
    "legacy_service_active_after_sha256",
    "legacy_service_local_health_sha256",
    "bridge_action_sha256",
    "route_contract_sha256",
    "original_caddy_sha256",
    "approval_bridge_caddy_sha256",
    "active_route_projection_sha256",
    "default_local_v1_route_preserved",
    "exact_v2_approval_routes_only",
    "caddy_validated",
    "caddy_reloaded",
    "caddy_readback_verified",
    "rollback_mode",
    "control_plane_mutation_performed",
    "source_data_mutation_performed",
    "production_host_mutation_performed",
    "caller_selected_input_accepted",
    "secret_material_recorded",
    "secret_digest_recorded",
    "activated_at_unix",
    "receipt_sha256",
})
PRODUCTION_PROJECT = cutover.PROJECT
PRODUCTION_PROJECT_NUMBER = "39589465056"
PRODUCTION_ZONE = cutover.ZONE
PRODUCTION_VM_NAME = cutover.VM_NAME
PRODUCTION_VM_INSTANCE_ID = "1094477181810932795"
PRODUCTION_OS_LOGIN_USERNAME = "lomliev_adventico_com"
PRODUCTION_OS_LOGIN_PROFILE_ID = "114674870412628413680"
_COLLECTOR_FIELDS = frozenset({
    "schema",
    "release_revision",
    "target",
    "artifacts",
    "gateway_before",
    "writer_before",
    "connector_before",
    "gateway_target_identity",
    "writer_target_identity",
    "connector_target_identity",
    "host_transition",
    "capability_topology",
    "initial_snapshot",
    "cron_inventory",
    "cron_continuity_plan",
    "mechanical_job_host_facts",
    "mechanical_job_package",
    "observed_at_unix",
    "source_boot_id_sha256",
    "secret_material_recorded",
    "secret_digest_recorded",
    "receipt_sha256",
})
_INITIAL_COLLECTOR_FIELDS = frozenset({
    "schema",
    "release_revision",
    "target",
    "artifacts",
    "gateway_before",
    "writer_before",
    "connector_before",
    "initial_snapshot",
    "cron_inventory",
    "cron_continuity_plan",
    "mechanical_job_host_facts",
    "mechanical_job_package",
    "observed_at_unix",
    "source_boot_id_sha256",
    "secret_material_recorded",
    "secret_digest_recorded",
    "receipt_sha256",
})
_STOPPED_COLLECTOR_FIELDS = frozenset({
    "schema",
    "release_revision",
    "freeze_plan_sha256",
    "freeze_approval_sha256",
    "gateway_stopped",
    "writer_stopped",
    "connector_stopped",
    "observed_at_unix",
    "source_boot_id_sha256",
    "secret_material_recorded",
    "secret_digest_recorded",
    "receipt_sha256",
})
_CONVERGENCE_FIELDS = frozenset({
    "schema",
    "release_revision",
    "freeze_plan_sha256",
    "cutover_plan_sha256",
    "preflight_receipt_sha256",
    "caddy_prepare_receipt_sha256",
    "maintenance_arm_receipt_sha256",
    "cutover_terminal_receipt_sha256",
    "caddy_terminal_receipt_sha256",
    "caddy_outcome",
    "legacy_service_retirement_receipt_sha256",
    "control_plane_mutation_performed",
    "source_data_mutation_performed",
    "production_host_mutation_performed",
    "secret_material_recorded",
    "secret_digest_recorded",
    "receipt_sha256",
})
_WORKFLOW_RECEIPT_FIELDS = frozenset({
    "schema",
    "release_revision",
    "freeze_plan_sha256",
    "freeze_approval_sha256",
    "cutover_plan_sha256",
    "convergence_receipt_sha256",
    "terminal_receipt_sha256",
    "caddy_prepare_receipt_sha256",
    "caddy_terminal_receipt_sha256",
    "caddy_outcome",
    "legacy_service_retirement_receipt_sha256",
    "gates",
    "private_key_staged",
    "control_plane_mutation_performed",
    "source_data_mutation_performed",
    "production_host_mutation_performed",
    "secret_material_recorded",
    "secret_digest_recorded",
    "receipt_sha256",
})


class OwnerCutoverError(RuntimeError):
    """One stable owner-side cutover authoring failure."""


def _active_owner_runtime_attestation(
    release_revision: str,
) -> Mapping[str, Any]:
    try:
        return production_owner_runtime.require_active_owner_runtime(
            release_revision
        )
    except production_owner_runtime.ProductionOwnerRuntimeError as exc:
        raise OwnerCutoverError("owner_cutover_runtime_not_active") from exc


class PinnedProductionGoogleComputeKnownHosts(
    canary_transport.PinnedGoogleComputeKnownHosts
):
    """Pin the SSH host key for the one production instance identity."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        private_key: str | os.PathLike[str] | None = None,
        public_key: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__(
            path,
            private_key=private_key,
            public_key=public_key,
            expected_instance_id=PRODUCTION_VM_INSTANCE_ID,
        )


class ProductionCutoverTransport(canary_transport.IapStoppedReleaseTransport):
    """Pinned IAP transport for the fixed production cutover entry points."""

    _ROTATION_STAGER_WRAPPER = (
        "/usr/libexec/muncho-release-foundation-exec-v3"
    )
    _ROTATION_STAGER_ACTIONS = frozenset({
        "rotate-unit-input-authority",
        "prepare-release-unit-inputs",
        "preauthorize-release-unit-inputs",
    })
    _PRODUCTION_INGRESS_INPUT_MAX_BYTES = 512 * 1024
    _PRODUCTION_INGRESS_OUTPUT_MAX_BYTES = 2 * 1024 * 1024
    _PRODUCTION_INGRESS_TIMEOUT_SECONDS = 120.0
    _ACTIONS = frozenset({
        "stage-host-artifacts",
        "collect-initial",
        "collect-host-plan",
        "collect-authority",
        "prepare-bridge",
        "activate-bridge",
        "stage-publication",
        "rotate-unit-input-authority",
        "prepare-release-unit-inputs",
        "preauthorize-release-unit-inputs",
        "stage-cron-continuity",
        "capture-final-tail",
        "collect-stopped",
        "phase-b-preflight",
        "prepare-caddy-cutover",
        "apply-cutover",
        "commit-caddy-cutover",
        "converge-cutover",
        "collect-bootstrap-target",
        "abort-freeze",
    })

    def __init__(
        self,
        owner_identity: Any,
        *,
        gcloud_executable: Any | None = None,
        gcloud_configuration: Any | None = None,
        known_hosts: Any | None = None,
        popen_factory: Any = subprocess.Popen,
        preflight_runner: Any = subprocess.run,
        preflight_timeout_seconds: float = 30.0,
    ) -> None:
        process_isolated_preflight_runner = preflight_runner is subprocess.run
        super().__init__(
            owner_identity,
            gcloud_executable=gcloud_executable,
            gcloud_configuration=gcloud_configuration,
            known_hosts=(
                known_hosts
                if known_hosts is not None
                else PinnedProductionGoogleComputeKnownHosts()
            ),
            popen_factory=popen_factory,
            preflight_runner=preflight_runner,
            preflight_timeout_seconds=preflight_timeout_seconds,
        )
        self._process_isolated_preflight_runner = (
            process_isolated_preflight_runner
        )

    def install_production_storage_growth_guest(
        self,
        *,
        release_sha: str,
        request: Mapping[str, Any],
        account: str,
    ) -> Mapping[str, Any]:
        """Install one digest-bound root guest entrypoint; no sudoers grant."""

        from scripts.canary import production_storage_growth_installer as installer

        expected = installer.build_guest_install_request(release_sha)
        if request != expected:
            raise OwnerCutoverError(
                "owner_cutover_production_storage_install_invalid"
            )
        source_root = self._prepare_source(release_sha, account=account)
        remote_installer = (
            f"{source_root}/scripts/canary/"
            "production_storage_growth_installer.py"
        )
        completed = self._run_remote_input(
            (
                "/usr/bin/python3",
                "-I",
                "-S",
                "-B",
                remote_installer,
                "install-guest",
            ),
            account=account,
            input_bytes=installer.canonical_json_bytes(expected),
            maximum_input_bytes=64 * 1024,
            maximum_output_bytes=64 * 1024,
            timeout_seconds=300.0,
        )
        raw = completed.stdout
        if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
            raise OwnerCutoverError(
                "owner_cutover_production_storage_install_invalid"
            )
        readiness = installer.decode_canonical_json(raw[:-1])
        try:
            return installer.validate_guest_readiness(
                readiness,
                release_sha=release_sha,
                guest_source_sha256=expected["guest_source_sha256"],
                installer_sha256=expected["installer_sha256"],
            )
        except installer.ProductionStorageInstallerError:
            raise OwnerCutoverError(
                "owner_cutover_production_storage_install_invalid"
            ) from None

    def _remote_argv(
        self,
        remote_argv: Sequence[str],
        *,
        account: str,
    ) -> tuple[str, ...]:
        if (
            not remote_argv
            or any(
                not isinstance(item, str)
                or not item
                or canary_transport._CONTROL_RE.search(item) is not None
                for item in remote_argv
            )
            or not isinstance(account, str)
            or canary_transport.GcloudOwnerAccessToken._ACCOUNT.fullmatch(account)
            is None
        ):
            raise canary_transport.OwnerLauncherError(
                "stopped_release_remote_argv_invalid"
            )
        command_prefix = self._gcloud_executable.trusted_command_prefix()
        if (
            len(command_prefix)
            != len(canary_transport._GCLOUD_PYTHON_ISOLATION_ARGS) + 2
            or command_prefix[1:-1]
            != canary_transport._GCLOUD_PYTHON_ISOLATION_ARGS
        ):
            raise canary_transport.OwnerLauncherError(
                "invalid_gcloud_command_prefix"
            )
        self._gcloud_configuration.assert_stable()
        known_hosts = self._known_hosts.absolute_path()
        private_key = self._known_hosts.private_key_path()
        self._known_hosts.public_key_line()
        if self._owner_identity.account_for_read_only_preflight() != account:
            raise canary_transport.OwnerLauncherError(
                "stopped_release_owner_identity_changed"
            )
        remote_command = shlex.join((
            "/usr/bin/sudo",
            "--non-interactive",
            "--",
            *remote_argv,
        ))
        return (
            *command_prefix,
            "compute",
            "ssh",
            f"{PRODUCTION_OS_LOGIN_USERNAME}@{PRODUCTION_VM_NAME}",
            f"--project={PRODUCTION_PROJECT}",
            f"--zone={PRODUCTION_ZONE}",
            f"--account={account}",
            "--plain",
            "--tunnel-through-iap",
            "--quiet",
            f"--command={remote_command}",
            *self._ssh_flags(known_hosts, private_key),
        )

    def _authorization_snapshot(self, account: str) -> tuple[str, str, str]:
        commands = {
            "instance": (
                "compute",
                "instances",
                "describe",
                PRODUCTION_VM_NAME,
                f"--project={PRODUCTION_PROJECT}",
                f"--zone={PRODUCTION_ZONE}",
                f"--account={account}",
                "--format=json(id,name,zone,metadata.items)",
                "--quiet",
            ),
            "project": (
                "compute",
                "project-info",
                "describe",
                f"--project={PRODUCTION_PROJECT}",
                f"--account={account}",
                "--format=json(name,commonInstanceMetadata.items)",
                "--quiet",
            ),
            "profile": (
                "compute",
                "os-login",
                "describe-profile",
                f"--project={PRODUCTION_PROJECT}",
                f"--account={account}",
                "--format=json",
                "--quiet",
            ),
        }
        values: dict[str, Any] = {}
        failures: dict[str, BaseException] = {}
        ordered = ("instance", "project", "profile")
        if self._process_isolated_preflight_runner:
            # Each future invokes the exact process runner independently. No
            # mutable SDK client/session is shared across worker threads.
            with ThreadPoolExecutor(
                max_workers=3,
                thread_name_prefix="production-iap-authorization",
            ) as pool:
                futures = {
                    name: pool.submit(
                        self._run_read_only_gcloud_json,
                        commands[name],
                    )
                    for name in ordered
                }
                for name in ordered:
                    try:
                        values[name] = futures[name].result()
                    except BaseException as exc:
                        failures[name] = exc
        else:
            # The injected test seam may close over mutable state, so preserve
            # the original deterministic single-threaded contract.
            for name in ordered:
                try:
                    values[name] = self._run_read_only_gcloud_json(
                        commands[name]
                    )
                except BaseException as exc:
                    failures[name] = exc
                    break
        if failures:
            raise failures[
                next(name for name in ordered if name in failures)
            ]
        instance = values["instance"]
        project = values["project"]
        profile = values["profile"]
        instance_metadata = self._metadata_items(instance, "metadata")
        if (
            set(instance) != {"id", "name", "zone", "metadata"}
            or instance.get("id") != PRODUCTION_VM_INSTANCE_ID
            or instance.get("name") != PRODUCTION_VM_NAME
            or instance.get("zone")
            != (
                "https://www.googleapis.com/compute/v1/projects/"
                f"{PRODUCTION_PROJECT}/zones/{PRODUCTION_ZONE}"
            )
            or dict(instance_metadata).get("enable-oslogin") != "TRUE"
            or "ssh-keys" in dict(instance_metadata)
        ):
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_authorization_invalid"
            )

        self._metadata_items(project, "commonInstanceMetadata")
        if (
            set(project) != {"name", "commonInstanceMetadata"}
            or project.get("name") != PRODUCTION_PROJECT
        ):
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_authorization_invalid"
            )

        posix_accounts = profile.get("posixAccounts")
        ssh_keys = profile.get("sshPublicKeys")
        public_key = self._known_hosts.public_key_line()
        if (
            set(profile) != {"name", "posixAccounts", "sshPublicKeys"}
            or profile.get("name") != PRODUCTION_OS_LOGIN_PROFILE_ID
            or not isinstance(posix_accounts, list)
            or not isinstance(ssh_keys, Mapping)
        ):
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_authorization_invalid"
            )
        matching_accounts = [
            item
            for item in posix_accounts
            if isinstance(item, Mapping)
            and item.get("username") == PRODUCTION_OS_LOGIN_USERNAME
            and item.get("primary") is True
            and item.get("operatingSystemType") == "LINUX"
            and item.get("homeDirectory")
            == f"/home/{PRODUCTION_OS_LOGIN_USERNAME}"
        ]
        matching_keys = [
            item
            for fingerprint, item in ssh_keys.items()
            if isinstance(fingerprint, str)
            and re.fullmatch(r"[0-9a-f]{64}", fingerprint) is not None
            and isinstance(item, Mapping)
            and item.get("fingerprint") == fingerprint
            and item.get("key") == public_key
        ]
        if len(matching_accounts) != 1 or len(matching_keys) != 1:
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_authorization_invalid"
            )
        if any(not isinstance(item, Mapping) for item in posix_accounts) or any(
            not isinstance(key, str) or not isinstance(item, Mapping)
            for key, item in ssh_keys.items()
        ):
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_authorization_invalid"
            )
        normalized_instance = {
            "id": PRODUCTION_VM_INSTANCE_ID,
            "name": PRODUCTION_VM_NAME,
            "zone": instance["zone"],
            "metadata": [
                {"key": key, "value": value}
                for key, value in instance_metadata
            ],
        }
        project_metadata = self._metadata_items(
            project,
            "commonInstanceMetadata",
        )
        normalized_project = {
            "name": PRODUCTION_PROJECT,
            "metadata": [
                {"key": key, "value": value}
                for key, value in project_metadata
            ],
        }
        normalized_profile = {
            "name": PRODUCTION_OS_LOGIN_PROFILE_ID,
            "posixAccounts": sorted(
                (dict(item) for item in posix_accounts),
                key=canary_transport._canonical_bytes,
            ),
            "sshPublicKeys": [
                {"fingerprint": fingerprint, "value": dict(item)}
                for fingerprint, item in sorted(ssh_keys.items())
            ],
        }
        self._postflight()
        return (
            canary_transport._sha256(
                canary_transport._canonical_bytes(normalized_instance)
            ),
            canary_transport._sha256(
                canary_transport._canonical_bytes(normalized_project)
            ),
            canary_transport._sha256(
                canary_transport._canonical_bytes(normalized_profile)
            ),
        )

    def _validate_dry_run(self, argv: Sequence[str]) -> None:
        command_prefix = self._gcloud_executable.trusted_command_prefix()
        environment = canary_transport._owner_gcloud_environment(
            self._gcloud_configuration,
            command_prefix[0],
        )
        try:
            completed = self._preflight_runner(
                (*argv, "--dry-run"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(environment),
                shell=False,
                timeout=self._preflight_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self._postflight()
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_dry_run_unavailable"
            ) from None
        self._postflight()
        if (
            completed.returncode != 0
            or not isinstance(completed.stdout, bytes)
            or not completed.stdout.endswith(b"\n")
            or b"\n" in completed.stdout[:-1]
            or len(completed.stdout) > 256 * 1024
        ):
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_dry_run_invalid"
            )
        try:
            rendered = completed.stdout[:-1].decode("utf-8", errors="strict")
            observed = tuple(shlex.split(rendered, posix=True))
        except (UnicodeError, ValueError):
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_dry_run_invalid"
            ) from None
        known_hosts = self._known_hosts.absolute_path()
        private_key = self._known_hosts.private_key_path()
        self._known_hosts.public_key_line()
        remote = next(
            (item.split("=", 1)[1] for item in argv if item.startswith("--command=")),
            None,
        )
        proxy = "ProxyCommand " + " ".join((
            *command_prefix,
            "compute",
            "start-iap-tunnel",
            PRODUCTION_VM_NAME,
            "%p",
            "--listen-on-stdin",
            f"--project={PRODUCTION_PROJECT}",
            f"--zone={PRODUCTION_ZONE}",
            "--verbosity=error",
        ))
        ssh_options = tuple(
            item.removeprefix("--ssh-flag=")
            for item in self._ssh_flags(known_hosts, private_key)
        )
        expected = (
            "/usr/bin/ssh",
            "-T",
            "-o",
            proxy,
            "-o",
            "ProxyUseFdpass=no",
            *ssh_options,
            (
                f"{PRODUCTION_OS_LOGIN_USERNAME}@"
                f"compute.{PRODUCTION_VM_INSTANCE_ID}"
            ),
            "--",
            *(() if remote is None else remote.split(" ")),
        )
        if remote is None or observed != expected:
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_dry_run_invalid"
            )

    def _run_production_ingress_observer_input(
        self,
        remote_argv: Sequence[str],
        *,
        account: str,
        input_bytes: bytes,
    ) -> tuple[subprocess.CompletedProcess[bytes], tuple[str, str, str]]:
        """Run the one read-only ingress observer under one bound authority.

        The generic stopped-release transport retains its three authorization
        snapshots because it also carries mutation commands.  This narrower
        surface returns the exact snapshot it held before and after the fixed
        read-only observer, avoiding duplicate outer snapshots without
        caching or weakening the pre-/post-execution fences.
        """

        prefix = self._fixed_remote_environment(chdir="/")
        logical = tuple(remote_argv)
        suffix = logical[len(prefix) :]
        if (
            logical[: len(prefix)] != prefix
            or len(suffix) != 9
            or suffix[:4] != ("/usr/bin/python3", "-B", "-I", "-")
            or suffix[4] not in {"inert", "post_iam"}
            or suffix[5] != "--release-revision"
            or package.REVISION.fullmatch(suffix[6] or "") is None
            or suffix[7] != "--plan-sha256"
            or canary_transport._SHA256.fullmatch(suffix[8] or "") is None
            or not isinstance(input_bytes, bytes)
            or not 0 < len(input_bytes)
            <= self._PRODUCTION_INGRESS_INPUT_MAX_BYTES
            or not isinstance(account, str)
            or canary_transport.GcloudOwnerAccessToken._ACCOUNT.fullmatch(
                account
            )
            is None
        ):
            raise canary_transport.OwnerLauncherError(
                "production_ingress_observer_input_invalid"
            )
        self._owner_identity.require_stable()
        authorization = self._authorization_snapshot(account)
        argv = self._remote_argv(logical, account=account)
        self._validate_dry_run(argv)
        if self._authorization_snapshot(account) != authorization:
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_authorization_changed"
            )
        command_prefix = self._gcloud_executable.trusted_command_prefix()
        try:
            completed = self._preflight_runner(
                argv,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(
                    canary_transport._owner_gcloud_environment(
                        self._gcloud_configuration,
                        command_prefix[0],
                    )
                ),
                shell=False,
                timeout=self._PRODUCTION_INGRESS_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self._postflight()
            raise canary_transport.OwnerLauncherError(
                "production_ingress_observer_unavailable"
            ) from None
        self._postflight()
        self._owner_identity.require_stable()
        if self._authorization_snapshot(account) != authorization:
            raise canary_transport.OwnerLauncherError(
                "iap_ssh_authorization_changed"
            )
        if (
            completed.returncode != 0
            or not isinstance(completed.stdout, bytes)
            or len(completed.stdout)
            > self._PRODUCTION_INGRESS_OUTPUT_MAX_BYTES
        ):
            raise canary_transport.OwnerLauncherError(
                "production_ingress_observer_failed"
            )
        return completed, authorization

    @classmethod
    def _remote_command(cls, revision: str, action: str) -> tuple[str, ...]:
        if package.REVISION.fullmatch(revision or "") is None or action not in cls._ACTIONS:
            raise OwnerCutoverError("owner_cutover_remote_command_invalid")
        interpreter = (
            f"{cutover.PRODUCTION_RELEASE_BASE}/"
            f"hermes-agent-{revision[:12]}/.venv/bin/python"
        )
        prefix = cls._fixed_remote_environment(chdir="/")
        if action in {"collect-initial", "collect-stopped"}:
            return (
                *prefix,
                interpreter,
                "-B",
                "-I",
                "-m",
                "scripts.canary.production_cutover_initial_collector",
                "initial" if action == "collect-initial" else "stopped",
                "--revision",
                revision,
            )
        if action in {"stage-host-artifacts", "collect-host-plan"}:
            return (
                *prefix,
                interpreter,
                "-B",
                "-I",
                "-m",
                "scripts.canary.production_cutover_host_plan",
                "stage" if action == "stage-host-artifacts" else "collect",
                "--revision",
                revision,
            )
        if action == "collect-authority":
            return (
                *prefix,
                interpreter,
                "-B",
                "-I",
                "-m",
                "scripts.canary.production_cutover_host_authority",
            )
        if action == "stage-publication":
            return (
                *prefix,
                interpreter,
                "-B",
                "-I",
                "-m",
                "scripts.canary.production_cutover_public_stager",
            )
        if action in cls._ROTATION_STAGER_ACTIONS:
            return (
                *prefix,
                cls._ROTATION_STAGER_WRAPPER,
                revision,
                action,
            )
        if action in {"prepare-bridge", "activate-bridge"}:
            return (
                *prefix,
                interpreter,
                "-B",
                "-I",
                "-m",
                "scripts.canary.owner_gate_caddy_cutover",
                action,
            )
        if action == "stage-cron-continuity":
            return (
                *prefix,
                interpreter,
                "-B",
                "-I",
                "-m",
                "scripts.canary.stage_production_cron_continuity",
                "stage",
                "--revision",
                revision,
            )
        if action in {
            "prepare-caddy-cutover",
            "commit-caddy-cutover",
            "converge-cutover",
        }:
            phase = {
                "prepare-caddy-cutover": "prepare",
                "commit-caddy-cutover": "commit",
                "converge-cutover": "converge",
            }[action]
            return (
                *prefix,
                interpreter,
                "-B",
                "-I",
                "-m",
                "scripts.canary.owner_gate_caddy_cutover",
                phase,
            )
        if action == "collect-bootstrap-target":
            return (
                *prefix,
                interpreter,
                "-B",
                "-I",
                "-m",
                "scripts.canary.production_initial_release_bootstrap",
                "collect-target-active",
                "--revision",
                revision,
            )
        return (
            *prefix,
            interpreter,
            "-B",
            "-I",
            "-m",
            "gateway.canonical_writer_production_cutover",
            action,
        )

    def invoke(
        self,
        revision: str,
        action: str,
        *,
        publication: Mapping[str, Any] | None = None,
        authority_request: Mapping[str, Any] | None = None,
        initial_receipt: Mapping[str, Any] | None = None,
        bridge_bootstrap_input: Mapping[str, Any] | None = None,
        release_rotation_request: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        account = self._owner_identity.account_for_read_only_preflight()
        self._owner_identity.require_stable()
        command = self._remote_command(revision, action)
        input_actions = {
            "stage-publication",
            "rotate-unit-input-authority",
            "collect-host-plan",
            "collect-authority",
            "prepare-bridge",
            "activate-bridge",
            "prepare-release-unit-inputs",
            "preauthorize-release-unit-inputs",
        }
        if action in input_actions:
            if action in {
                "stage-publication",
                "rotate-unit-input-authority",
            }:
                input_value = publication
            elif action == "collect-host-plan":
                input_value = initial_receipt
            elif action == "collect-authority":
                input_value = authority_request
            elif action in {
                "prepare-release-unit-inputs",
                "preauthorize-release-unit-inputs",
            }:
                input_value = release_rotation_request
            else:
                input_value = bridge_bootstrap_input
            if input_value is None:
                raise OwnerCutoverError("owner_cutover_publication_missing")
            if (
                sum(
                    item is not None
                    for item in (
                        publication,
                        authority_request,
                        initial_receipt,
                        bridge_bootstrap_input,
                        release_rotation_request,
                    )
                )
                != 1
            ):
                raise OwnerCutoverError("owner_cutover_publication_unexpected")
            frame = _canonical(input_value)
            completed = self._run_remote_input(
                command,
                account=account,
                input_bytes=frame,
                timeout_seconds=900,
                maximum_input_bytes=(
                    canary_transport._STOPPED_RELEASE_REMOTE_INPUT_MAX_BYTES
                ),
                maximum_output_bytes=(
                    canary_transport._STOPPED_RELEASE_REMOTE_OUTPUT_MAX_BYTES
                ),
            )
        else:
            if (
                publication is not None
                or authority_request is not None
                or initial_receipt is not None
                or bridge_bootstrap_input is not None
                or release_rotation_request is not None
            ):
                raise OwnerCutoverError("owner_cutover_publication_unexpected")
            completed = self._run_remote(
                command,
                account=account,
                timeout_seconds=2_400,
                maximum_output_bytes=(
                    canary_transport._STOPPED_RELEASE_REMOTE_OUTPUT_MAX_BYTES
                ),
            )
        self._owner_identity.require_stable()
        stdout = completed.stdout
        if (
            not isinstance(stdout, bytes)
            or not stdout.endswith(b"\n")
            or b"\n" in stdout[:-1]
        ):
            raise OwnerCutoverError("owner_cutover_remote_output_invalid")
        return _decode(stdout[:-1])


class ProductionCutoverBridgeBootstrap:
    """Drive only the two fixed remote Caddy bridge bootstrap phases."""

    def __init__(
        self,
        release_revision: str,
        transport: ProductionCutoverTransport,
    ) -> None:
        if (
            package.REVISION.fullmatch(release_revision or "") is None
            or not callable(getattr(transport, "invoke", None))
        ):
            raise OwnerCutoverError(
                "owner_cutover_bridge_bootstrap_invalid"
            )
        self._release_revision = release_revision
        self._transport = transport

    def prepare(self, document: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._transport.invoke(
            self._release_revision,
            "prepare-bridge",
            bridge_bootstrap_input=document,
        )

    def consume_and_install(
        self,
        document: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._transport.invoke(
            self._release_revision,
            "activate-bridge",
            bridge_bootstrap_input=document,
        )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_unix(value: Any) -> int:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise OwnerCutoverError("owner_cutover_operational_fact_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise OwnerCutoverError(
            "owner_cutover_operational_fact_invalid"
        ) from exc
    if parsed.tzinfo != timezone.utc or parsed.microsecond != 0:
        raise OwnerCutoverError("owner_cutover_operational_fact_invalid")
    return int(parsed.timestamp())


def _validate_operational_facts(
    value: Mapping[str, Any],
    *,
    release_revision: str,
    observed_at_unix: int,
    require_continuity_plan: bool,
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any] | None,
]:
    try:
        inventory = cron_migration.validate_inventory(value["cron_inventory"])
        facts_candidate = value["mechanical_job_host_facts"]
        facts = mechanical_job_rail.validate_host_facts(
            facts_candidate,
            expected_sha256=str(facts_candidate.get("host_facts_sha256", "")),
        )
        mechanical_package = mechanical_job_rail.validate_package_manifest(
            value["mechanical_job_package"],
            revision=release_revision,
            host_facts_sha256=facts["host_facts_sha256"],
        )
        continuity_plan = None
        if require_continuity_plan:
            continuity_plan = cron_migration.validate_owner_approved_plan(
                inventory,
                value["cron_continuity_plan"],
                mechanical_package["manifest_sha256"],
            )
        timestamps = (
            _utc_unix(inventory["created_at"]),
            _utc_unix(facts["collected_at"]),
        )
        if any(
            item > observed_at_unix + 30
            or observed_at_unix - item > MAX_COLLECTOR_AGE_SECONDS
            for item in timestamps
        ):
            raise ValueError("operational fact is stale")
    except OwnerCutoverError:
        raise
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise OwnerCutoverError(
            "owner_cutover_operational_fact_invalid"
        ) from exc
    return inventory, facts, mechanical_package, continuity_plan


def _decode(raw: bytes) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, item in items:
            if name in result:
                raise OwnerCutoverError("owner_cutover_duplicate_key")
            result[name] = item
        return result

    def constant(_value: str) -> None:
        raise OwnerCutoverError("owner_cutover_nonfinite_number")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except OwnerCutoverError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise OwnerCutoverError("owner_cutover_json_invalid") from exc
    if not isinstance(value, Mapping) or raw != _canonical(value):
        raise OwnerCutoverError("owner_cutover_json_not_canonical")
    return value


def _read_public_bytes(path: Path) -> bytes:
    try:
        state = path.lstat()
        if (
            path.resolve(strict=True) != path
            or stat.S_ISLNK(state.st_mode)
            or not stat.S_ISREG(state.st_mode)
            or state.st_nlink != 1
            or state.st_uid != os.getuid()  # windows-footgun: ok — macOS/Linux owner boundary
            or stat.S_IMODE(state.st_mode) & 0o022
            or not 0 < state.st_size <= MAX_JSON
        ):
            raise OwnerCutoverError("owner_cutover_public_input_invalid")
        raw = path.read_bytes()
        after = path.lstat()
    except OwnerCutoverError:
        raise
    except OSError as exc:
        raise OwnerCutoverError("owner_cutover_public_input_unavailable") from exc
    if (
        len(raw) != state.st_size
        or (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise OwnerCutoverError("owner_cutover_public_input_changed")
    return raw


def _read_public_json(path: Path) -> Mapping[str, Any]:
    return _decode(_read_public_bytes(path))


def _read_public_json_line(path: Path) -> Mapping[str, Any]:
    raw = _read_public_bytes(path)
    if not raw.endswith(b"\n"):
        raise OwnerCutoverError("owner_cutover_json_not_canonical")
    return _decode(raw[:-1])


def load_owner_private_key(path: Path) -> Ed25519PrivateKey:
    """Read one pinned owner-local Ed25519 key without exporting its bytes."""

    try:
        state = path.lstat()
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or stat.S_ISLNK(state.st_mode)
            or not stat.S_ISREG(state.st_mode)
            or state.st_nlink != 1
            or state.st_uid != os.getuid()  # windows-footgun: ok — macOS/Linux owner boundary
            or stat.S_IMODE(state.st_mode) not in {0o400, 0o600}
            or not 0 < state.st_size <= 16 * 1024
        ):
            raise OwnerCutoverError("owner_cutover_private_key_invalid")
        raw = path.read_bytes()
        after = path.lstat()
    except OwnerCutoverError:
        raise
    except OSError as exc:
        raise OwnerCutoverError("owner_cutover_private_key_unavailable") from exc
    if (
        len(raw) != state.st_size
        or (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise OwnerCutoverError("owner_cutover_private_key_changed")
    try:
        if raw.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----\n"):
            key = serialization.load_ssh_private_key(raw, password=None)
        elif raw.startswith(b"-----BEGIN PRIVATE KEY-----\n"):
            key = serialization.load_pem_private_key(raw, password=None)
        else:
            raise ValueError("unsupported private-key encoding")
    except (TypeError, ValueError) as exc:
        raise OwnerCutoverError("owner_cutover_private_key_invalid") from exc
    finally:
        raw = b""
    if not isinstance(key, Ed25519PrivateKey):
        raise OwnerCutoverError("owner_cutover_private_key_invalid")
    return key


def _public_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ).hex()


def validate_collector_receipt(
    value: Mapping[str, Any],
    *,
    release_revision: str,
    now_unix: int | None = None,
) -> Mapping[str, Any]:
    """Validate fresh public observations without interpreting event meaning."""

    current = int(time.time()) if now_unix is None else now_unix
    if set(value) != _COLLECTOR_FIELDS:
        raise OwnerCutoverError("owner_cutover_collector_fields_invalid")
    unsigned = {name: item for name, item in value.items() if name != "receipt_sha256"}
    if (
        value.get("schema") != COLLECTOR_SCHEMA
        or value.get("release_revision") != release_revision
        or package.REVISION.fullmatch(release_revision) is None
        or type(value.get("observed_at_unix")) is not int
        or not current - MAX_COLLECTOR_AGE_SECONDS
        <= value["observed_at_unix"]
        <= current + 30
        or _SHA256.fullmatch(str(value.get("source_boot_id_sha256"))) is None
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise OwnerCutoverError("owner_cutover_collector_identity_invalid")
    try:
        gateway = cutover.ServiceObservation.from_mapping(value["gateway_before"])
        writer = cutover.ServiceObservation.from_mapping(value["writer_before"])
        connector = cutover.ServiceObservation.from_mapping(value["connector_before"])
        snapshot = cutover.LegacySnapshot.from_mapping(value["initial_snapshot"])
        _validate_operational_facts(
            value,
            release_revision=release_revision,
            observed_at_unix=value["observed_at_unix"],
            require_continuity_plan=True,
        )
        # The full cross-field host/artifact/topology proof is deliberately
        # exercised by the production builders below, not approximated here.
        if (
            gateway.value["name"] != cutover.GATEWAY_UNIT
            or gateway.stopped
            or writer.value["name"] != cutover.WRITER_UNIT
            or not writer.stopped
            or connector.value["name"] != cutover.CONNECTOR_UNIT
            or not connector.stopped
            or snapshot.value["observed_at_unix"] > value["observed_at_unix"]
            or any(
                item.value["observed_at_unix"] > value["observed_at_unix"] + 30
                or value["observed_at_unix"]
                - item.value["observed_at_unix"]
                > MAX_COLLECTOR_AGE_SECONDS
                for item in (gateway, writer, connector)
            )
        ):
            raise ValueError("collector state invalid")
    except (KeyError, TypeError, ValueError) as exc:
        raise OwnerCutoverError("owner_cutover_collector_content_invalid") from exc
    return copy.deepcopy(dict(value))


def validate_initial_collector_receipt(
    value: Mapping[str, Any],
    *,
    release_revision: str,
    now_unix: int | None = None,
) -> Mapping[str, Any]:
    """Validate the release-bound read-only receipt before host planning."""

    current = int(time.time()) if now_unix is None else now_unix
    if set(value) != _INITIAL_COLLECTOR_FIELDS:
        raise OwnerCutoverError("owner_cutover_initial_collector_fields_invalid")
    unsigned = {name: item for name, item in value.items() if name != "receipt_sha256"}
    if (
        value.get("schema") != INITIAL_COLLECTOR_SCHEMA
        or value.get("release_revision") != release_revision
        or package.REVISION.fullmatch(release_revision) is None
        or type(value.get("observed_at_unix")) is not int
        or not current - MAX_COLLECTOR_AGE_SECONDS
        <= value["observed_at_unix"]
        <= current + 30
        or _SHA256.fullmatch(str(value.get("source_boot_id_sha256"))) is None
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise OwnerCutoverError("owner_cutover_initial_collector_identity_invalid")
    try:
        cutover._validate_target(value["target"])
        artifacts = value["artifacts"]
        if not isinstance(artifacts, Mapping) or set(artifacts) != set(
            cutover._CUTOVER_ARTIFACT_NAMES
        ):
            raise ValueError("initial artifact fields invalid")
        for name in cutover._CUTOVER_ARTIFACT_NAMES:
            cutover._artifact(
                artifacts[name],
                f"initial collector {name}",
                release_revision,
            )
        gateway = cutover.ServiceObservation.from_mapping(value["gateway_before"])
        writer = cutover.ServiceObservation.from_mapping(value["writer_before"])
        connector = cutover.ServiceObservation.from_mapping(value["connector_before"])
        snapshot = cutover.LegacySnapshot.from_mapping(value["initial_snapshot"])
        observed = value["observed_at_unix"]
        _validate_operational_facts(
            value,
            release_revision=release_revision,
            observed_at_unix=observed,
            require_continuity_plan=True,
        )
        if (
            gateway.value["name"] != cutover.GATEWAY_UNIT
            or gateway.stopped
            or writer.value["name"] != cutover.WRITER_UNIT
            or not writer.stopped
            or connector.value["name"] != cutover.CONNECTOR_UNIT
            or not connector.stopped
            or any(
                item > observed + 30
                or observed - item > MAX_COLLECTOR_AGE_SECONDS
                for item in (
                    gateway.value["observed_at_unix"],
                    writer.value["observed_at_unix"],
                    connector.value["observed_at_unix"],
                    snapshot.value["observed_at_unix"],
                )
            )
        ):
            raise ValueError("initial collector state invalid")
    except (KeyError, TypeError, ValueError) as exc:
        raise OwnerCutoverError(
            "owner_cutover_initial_collector_content_invalid"
        ) from exc
    return copy.deepcopy(dict(value))


def validate_stopped_collector_receipt(
    value: Mapping[str, Any],
    *,
    freeze_plan: Mapping[str, Any],
    freeze_approval: Mapping[str, Any],
    now_unix: int | None = None,
    approval_now_unix: int | None = None,
) -> Mapping[str, Any]:
    current = int(time.time()) if now_unix is None else now_unix
    approval_current = (
        current if approval_now_unix is None else approval_now_unix
    )
    if set(value) != _STOPPED_COLLECTOR_FIELDS:
        raise OwnerCutoverError("owner_cutover_stopped_collector_fields_invalid")
    unsigned = {name: item for name, item in value.items() if name != "receipt_sha256"}
    try:
        plan = cutover.FreezePlan.from_mapping(freeze_plan)
        approval = cutover.CutoverApproval.from_mapping(
            freeze_approval,
            plan=plan,
            now_unix=approval_current,
        )
        gateway = cutover.ServiceObservation.from_mapping(
            value["gateway_stopped"]
        )
        writer = cutover.ServiceObservation.from_mapping(value["writer_stopped"])
        connector = cutover.ServiceObservation.from_mapping(
            value["connector_stopped"]
        )
        observed = value["observed_at_unix"]
        expected = (
            cutover.ServiceObservation.from_mapping(plan.value["gateway_before"]),
            cutover.ServiceObservation.from_mapping(plan.value["writer_before"]),
            cutover.ServiceObservation.from_mapping(plan.value["connector_before"]),
        )
        current_services = (gateway, writer, connector)
        if (
            value["schema"] != STOPPED_COLLECTOR_SCHEMA
            or value["release_revision"] != plan.value["release_revision"]
            or value["freeze_plan_sha256"] != plan.sha256
            or value["freeze_approval_sha256"]
            != approval.value["approval_sha256"]
            or type(observed) is not int
            or not current - MAX_COLLECTOR_AGE_SECONDS <= observed <= current + 30
            or _SHA256.fullmatch(str(value["source_boot_id_sha256"])) is None
            or value["secret_material_recorded"] is not False
            or value["secret_digest_recorded"] is not False
            or value["receipt_sha256"] != _sha(_canonical(unsigned))
            or any(not item.stopped for item in current_services)
            or any(
                item.stable_identity() != expected_item.stable_identity()
                for item, expected_item in zip(
                    current_services,
                    expected,
                    strict=True,
                )
            )
            or any(
                item.value["observed_at_unix"] > observed + 30
                or observed - item.value["observed_at_unix"]
                > MAX_COLLECTOR_AGE_SECONDS
                for item in current_services
            )
        ):
            raise ValueError("stopped collector state invalid")
    except (KeyError, TypeError, ValueError) as exc:
        raise OwnerCutoverError(
            "owner_cutover_stopped_collector_invalid"
        ) from exc
    return copy.deepcopy(dict(value))


def build_unit_input_authority(
    *,
    release_revision: str,
    unit_inputs: Mapping[str, Any],
    owner_subject_sha256: str,
    private_key: Ed25519PrivateKey,
    owner_runtime_attestation: Mapping[str, Any],
    now_unix: int | None = None,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    current = int(time.time()) if now_unix is None else now_unix
    public = _public_hex(private_key)
    plan = package.build_unit_input_plan(
        release_revision=release_revision,
        unit_inputs=unit_inputs,
        owner_subject_sha256=owner_subject_sha256,
        owner_public_key_ed25519_hex=public,
        owner_runtime_attestation=owner_runtime_attestation,
        created_at_unix=current,
    )
    approval = {
        "schema": package.UNIT_INPUT_APPROVAL_SCHEMA,
        "purpose": "production_cutover_unit_inputs",
        "plan_sha256": plan["plan_sha256"],
        "release_revision": release_revision,
        "owner_subject_sha256": owner_subject_sha256,
        "owner_public_key_ed25519_hex": public,
        "owner_key_id": plan["owner_key_id"],
        "nonce_sha256": _sha(secrets.token_bytes(32)),
        "issued_at_unix": current,
        "expires_at_unix": current + 900,
        "approved": True,
        "signature_ed25519_hex": "0" * 128,
        "approval_sha256": "0" * 64,
    }
    approval["signature_ed25519_hex"] = private_key.sign(
        package.unit_input_approval_signature_payload(approval)
    ).hex()
    approval["approval_sha256"] = _sha(_canonical({
        name: item for name, item in approval.items() if name != "approval_sha256"
    }))
    approval = package.validate_unit_input_approval(
        approval,
        plan=plan,
        now_unix=current,
    )
    publication = build_publication(
        action="unit-input-authority",
        release_revision=release_revision,
        documents={"plan": plan, "approval": approval},
        now_unix=current,
    )
    return plan, approval, publication


def sign_freeze_approval(
    plan: cutover.FreezePlan,
    *,
    private_key: Ed25519PrivateKey,
    now_unix: int | None = None,
    sequence: int = 0,
    previous_approval_sha256: str | None = None,
) -> Mapping[str, Any]:
    current = int(time.time()) if now_unix is None else now_unix
    if _public_hex(private_key) != plan.value["owner_public_key_ed25519_hex"]:
        raise OwnerCutoverError("owner_cutover_private_key_plan_mismatch")
    raw = {
        "schema": cutover.APPROVAL_SCHEMA,
        "plan_kind": "freeze",
        "purpose": f"freeze_{'apply' if sequence == 0 else 'resume'}",
        "sequence": sequence,
        "previous_approval_sha256": previous_approval_sha256,
        "plan_sha256": plan.sha256,
        "owner_subject_sha256": plan.value["owner_subject_sha256"],
        "owner_public_key_ed25519_hex": plan.value[
            "owner_public_key_ed25519_hex"
        ],
        "owner_key_id": plan.value["owner_key_id"],
        "nonce_sha256": _sha(secrets.token_bytes(32)),
        "issued_at_unix": current,
        "expires_at_unix": current + 900,
        "approved": True,
        "signature_ed25519_hex": "0" * 128,
        "approval_sha256": "0" * 64,
    }
    raw["signature_ed25519_hex"] = private_key.sign(
        cutover.approval_signature_payload(raw)
    ).hex()
    raw["approval_sha256"] = _sha(_canonical({
        name: item for name, item in raw.items() if name != "approval_sha256"
    }))
    return cutover.CutoverApproval.from_mapping(
        raw,
        plan=plan,
        now_unix=current,
    ).value


def author_freeze(
    *,
    collector_receipt: Mapping[str, Any],
    release_revision: str,
    owner_subject_sha256: str,
    private_key: Ed25519PrivateKey,
    owner_runtime_attestation: Mapping[str, Any],
    isolated_canary_goal_prerequisite: Mapping[str, Any],
    database_recovery_receipt: Mapping[str, Any],
    truth_mode: str,
    accepted_event_receipts: list[Mapping[str, Any]] | None = None,
    now_unix: int | None = None,
) -> tuple[cutover.FreezePlan, Mapping[str, Any], Mapping[str, Any]]:
    current = int(time.time()) if now_unix is None else now_unix
    receipt = validate_collector_receipt(
        collector_receipt,
        release_revision=release_revision,
        now_unix=current,
    )
    try:
        recovery_receipt = database_recovery.validate_receipt_for_freeze(
            database_recovery_receipt,
            release_revision=release_revision,
            now_unix=current,
        )
    except database_recovery.ProductionDatabaseRecoveryError as exc:
        raise OwnerCutoverError(
            "owner_cutover_database_recovery_invalid"
        ) from exc
    if truth_mode not in {"start_new_truth_epoch", "reseed_accepted_events"}:
        raise OwnerCutoverError("owner_cutover_truth_mode_invalid")
    if _SHA256.fullmatch(owner_subject_sha256) is None:
        raise OwnerCutoverError("owner_cutover_owner_subject_invalid")
    gateway = cutover.ServiceObservation.from_mapping(receipt["gateway_before"])
    writer = cutover.ServiceObservation.from_mapping(receipt["writer_before"])
    connector = cutover.ServiceObservation.from_mapping(receipt["connector_before"])
    snapshot = cutover.LegacySnapshot.from_mapping(receipt["initial_snapshot"])
    decision_uuid = str(uuid.uuid4())
    event_uuid = str(uuid.uuid4())
    epoch = (
        f"truth-epoch:{uuid.uuid4()}"
        if truth_mode == "start_new_truth_epoch"
        else None
    )
    decision = cutover.build_legacy_truth_decision(
        mode=truth_mode,
        decision_id=f"legacy-truth-decision:{decision_uuid}",
        decision_event_id=event_uuid,
        owner_subject_sha256=owner_subject_sha256,
        reviewed_snapshot=snapshot,
        accepted_event_receipts=accepted_event_receipts,
        truth_epoch_id=epoch,
    )
    authority = cutover.build_cutover_authority(
        release_revision=release_revision,
        artifacts=receipt["artifacts"],
        gateway_before=gateway,
        writer_before=writer,
        connector_before=connector,
        gateway_target_identity=receipt["gateway_target_identity"],
        writer_target_identity=receipt["writer_target_identity"],
        connector_target_identity=receipt["connector_target_identity"],
        host_transition=receipt["host_transition"],
        capability_topology=receipt["capability_topology"],
        cron_inventory=receipt["cron_inventory"],
        cron_continuity_plan=receipt["cron_continuity_plan"],
        mechanical_job_host_facts=receipt["mechanical_job_host_facts"],
        mechanical_job_package=receipt["mechanical_job_package"],
        isolated_canary_goal_prerequisite=(
            isolated_canary_goal_prerequisite
        ),
        database_recovery_receipt=recovery_receipt,
        legacy_truth_decision=decision,
        max_appended_rows=10_000,
        max_capture_delay_seconds=900,
    )
    plan = cutover.build_freeze_plan(
        release_revision=release_revision,
        target=receipt["target"],
        owner_subject_sha256=owner_subject_sha256,
        owner_public_key_ed25519_hex=_public_hex(private_key),
        gateway_before=gateway,
        writer_before=writer,
        connector_before=connector,
        initial_snapshot=snapshot,
        cutover_authority=authority,
        owner_runtime_attestation=owner_runtime_attestation,
    )
    approval = sign_freeze_approval(
        plan,
        private_key=private_key,
        now_unix=current,
    )
    publication = build_publication(
        action="freeze-authority",
        release_revision=release_revision,
        documents={"plan": plan.to_mapping(), "approval": approval},
        now_unix=current,
    )
    return plan, approval, publication


def author_cutover(
    *,
    freeze_plan: Mapping[str, Any],
    freeze_approval: Mapping[str, Any],
    final_tail_receipt: Mapping[str, Any],
    gateway_stopped: Mapping[str, Any],
    writer_stopped: Mapping[str, Any],
    connector_stopped: Mapping[str, Any],
    now_unix: int | None = None,
    approval_now_unix: int | None = None,
) -> tuple[cutover.CutoverPlan, Mapping[str, Any]]:
    current = int(time.time()) if now_unix is None else now_unix
    approval_current = (
        current if approval_now_unix is None else approval_now_unix
    )
    freeze = cutover.FreezePlan.from_mapping(freeze_plan)
    cutover.CutoverApproval.from_mapping(
        freeze_approval,
        plan=freeze,
        now_unix=approval_current,
    )
    tail = cutover.FinalTailReceipt.from_mapping(
        final_tail_receipt,
        plan=freeze,
    )
    if tail.value["approval_sha256"] != freeze_approval["approval_sha256"]:
        raise OwnerCutoverError("owner_cutover_tail_approval_mismatch")
    plan = cutover.build_cutover_plan(
        freeze_plan=freeze,
        final_tail_receipt=tail,
        gateway_stopped=cutover.ServiceObservation.from_mapping(gateway_stopped),
        writer_stopped=cutover.ServiceObservation.from_mapping(writer_stopped),
        connector_stopped=cutover.ServiceObservation.from_mapping(
            connector_stopped
        ),
    )
    publication = build_publication(
        action="cutover-plan",
        release_revision=plan.value["release_revision"],
        documents={"plan": plan.to_mapping()},
        now_unix=current,
    )
    return plan, publication


_HOST_AUTHORITY_PLAN_FIELDS = frozenset({
    "release_manifest_sha256",
    "gateway_target_identity",
    "writer_target_identity",
    "connector_target_identity",
    "host_transition",
    "capability_topology",
    "cron_continuity_plan",
})


def _validate_publication_stage_receipt(
    value: Mapping[str, Any],
    *,
    publication: Mapping[str, Any],
    expected_file_count: int,
) -> Mapping[str, Any]:
    fields = {
        "schema",
        "action",
        "release_revision",
        "publication_sha256",
        "files",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
    unsigned = {
        name: item for name, item in value.items() if name != "receipt_sha256"
    }
    files = value.get("files")
    action = publication.get("action")
    documents = publication.get("documents")
    if not isinstance(documents, Mapping):
        raise OwnerCutoverError("owner_cutover_publication_stage_invalid")
    if action == "unit-input-authority" and set(documents) == {"plan", "approval"}:
        expected_files = (
            (
                str(package.STAGED_UNIT_INPUT_PLAN_PATH),
                _sha(_canonical(documents["plan"])),
            ),
            (
                str(package.STAGED_UNIT_INPUT_APPROVAL_PATH),
                _sha(_canonical(documents["approval"])),
            ),
        )
    elif action == "freeze-authority" and set(documents) == {"plan", "approval"}:
        expected_files = (
            (
                str(cutover.STAGED_FREEZE_PLAN_PATH),
                _sha(_canonical(documents["plan"])),
            ),
            (
                str(cutover.STAGED_FREEZE_APPROVAL_PATH),
                _sha(_canonical(documents["approval"])),
            ),
        )
    elif action == "cutover-plan" and set(documents) == {"plan"}:
        expected_files = ((
            str(cutover.STAGED_CUTOVER_PLAN_PATH),
            _sha(_canonical(documents["plan"])),
        ),)
    else:
        raise OwnerCutoverError("owner_cutover_publication_stage_invalid")
    if (
        set(value) != fields
        or value.get("schema")
        != "muncho-production-cutover-publication-receipt.v1"
        or value.get("action") != publication.get("action")
        or value.get("release_revision") != publication.get("release_revision")
        or value.get("publication_sha256")
        != publication.get("publication_sha256")
        or not isinstance(files, list)
        or len(files) != expected_file_count
        or len(files) != len(expected_files)
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"path", "sha256", "created"}
            or not isinstance(item["path"], str)
            or not item["path"].startswith(
                str(cutover.EVIDENCE_ROOT / "staged") + "/"
            )
            or _SHA256.fullmatch(str(item["sha256"])) is None
            or type(item["created"]) is not bool
            for item in files
        )
        or tuple((item["path"], item["sha256"]) for item in files)
        != expected_files
        or len({item["path"] for item in files}) != len(files)
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise OwnerCutoverError("owner_cutover_publication_stage_invalid")
    return copy.deepcopy(dict(value))


def stage_unit_input_authority(
    *,
    release_revision: str,
    remote_stager_revision: str,
    publication: Mapping[str, Any],
    transport: Any,
    now_unix: int | None = None,
) -> Mapping[str, Any]:
    """Stage successor unit inputs through one installed compatible runtime.

    The owner launcher itself remains attested to ``release_revision``.  Only
    the fixed remote public stager is selected from
    ``remote_stager_revision`` so the successor's signed unit inputs can be
    present before that successor release is packaged or installed.
    """

    documents = publication.get("documents") if isinstance(
        publication, Mapping
    ) else None
    if (
        package.REVISION.fullmatch(release_revision or "") is None
        or package.REVISION.fullmatch(remote_stager_revision or "") is None
        or not isinstance(documents, Mapping)
        or publication.get("action") != "unit-input-authority"
        or publication.get("release_revision") != release_revision
        or set(documents) != {"plan", "approval"}
        or not callable(getattr(transport, "invoke", None))
    ):
        raise OwnerCutoverError("owner_cutover_unit_input_stage_invalid")
    try:
        expected_publication = build_publication(
            action="unit-input-authority",
            release_revision=release_revision,
            documents={
                "plan": documents["plan"],
                "approval": documents["approval"],
            },
            now_unix=now_unix,
        )
    except (KeyError, PermissionError, PublicStagingError, TypeError, ValueError):
        raise OwnerCutoverError(
            "owner_cutover_unit_input_stage_invalid"
        ) from None
    if publication != expected_publication:
        raise OwnerCutoverError("owner_cutover_unit_input_stage_invalid")
    return _validate_publication_stage_receipt(
        transport.invoke(
            remote_stager_revision,
            "stage-publication",
            publication=expected_publication,
        ),
        publication=expected_publication,
        expected_file_count=2,
    )


def rotate_unit_input_authority(
    *,
    release_revision: str,
    remote_stager_revision: str,
    publication: Mapping[str, Any],
    transport: Any,
    now_unix: int | None = None,
) -> Mapping[str, Any]:
    """Rotate a complete predecessor triplet through an installed runtime."""

    documents = publication.get("documents") if isinstance(
        publication, Mapping
    ) else None
    if (
        package.REVISION.fullmatch(release_revision or "") is None
        or package.REVISION.fullmatch(remote_stager_revision or "") is None
        or not isinstance(documents, Mapping)
        or publication.get("action") != "unit-input-authority"
        or publication.get("release_revision") != release_revision
        or set(documents) != {"plan", "approval"}
        or not callable(getattr(transport, "invoke", None))
    ):
        raise OwnerCutoverError("owner_cutover_unit_input_rotation_invalid")
    approval = documents.get("approval")
    issued_at = (
        approval.get("issued_at_unix")
        if isinstance(approval, Mapping)
        else None
    )
    if type(issued_at) is not int:
        raise OwnerCutoverError("owner_cutover_unit_input_rotation_invalid")
    try:
        # The remote edge owns the freshness decision after it acquires the
        # activation lock.  Here we authenticate the immutable publication at
        # issuance so an exact completed replay remains possible after expiry.
        expected_publication = build_publication(
            action="unit-input-authority",
            release_revision=release_revision,
            documents={
                "plan": documents["plan"],
                "approval": documents["approval"],
            },
            now_unix=issued_at,
        )
    except (KeyError, PermissionError, PublicStagingError, TypeError, ValueError):
        raise OwnerCutoverError(
            "owner_cutover_unit_input_rotation_invalid"
        ) from None
    if publication != expected_publication:
        raise OwnerCutoverError("owner_cutover_unit_input_rotation_invalid")
    try:
        return unit_rotation.validate_rotation_receipt(
            transport.invoke(
                remote_stager_revision,
                "rotate-unit-input-authority",
                publication=expected_publication,
            ),
            publication=expected_publication,
        )
    except unit_rotation.UnitInputRotationError:
        raise OwnerCutoverError(
            "owner_cutover_unit_input_rotation_invalid"
        ) from None


def build_release_unit_input_phase_request(
    *,
    action: str,
    owner_release_revision: str,
    remote_stager_revision: str,
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    prepared_receipt: Mapping[str, Any] | None = None,
    preauthorization_receipt: Mapping[str, Any] | None = None,
    expected_transaction_sha256: str | None = None,
) -> Mapping[str, Any]:
    """Build and fully validate one immutable split-phase request."""

    if (
        action not in unit_rotation.RELEASE_PHASE_ACTIONS
        or package.REVISION.fullmatch(owner_release_revision or "") is None
        or package.REVISION.fullmatch(remote_stager_revision or "") is None
        or not isinstance(unit_input_publication, Mapping)
        or not isinstance(release_update_publication, Mapping)
        or unit_input_publication.get("release_revision")
        != remote_stager_revision
        or release_update_publication.get("release_revision")
        != remote_stager_revision
        or not isinstance(trusted_predecessor, Mapping)
        or not isinstance(expected_predecessor_trust_sha256, str)
        or _SHA256.fullmatch(expected_predecessor_trust_sha256)
        is None
    ):
        raise OwnerCutoverError(
            "owner_cutover_release_unit_input_phase_invalid"
        )
    validated_prepared: Mapping[str, Any] | None = None
    validated_preauthorization: Mapping[str, Any] | None = None
    try:
        if action != "prepare-release-unit-inputs":
            if (
                not isinstance(prepared_receipt, Mapping)
                or _SHA256.fullmatch(
                    str(expected_transaction_sha256 or "")
                )
                is None
            ):
                raise unit_rotation.UnitInputRotationError(
                    "unit_input_rotation_phase_request_invalid"
                )
            validated_prepared = (
                unit_rotation.validate_release_prepared_rotation_receipt(
                    prepared_receipt,
                    unit_input_publication=unit_input_publication,
                    release_update_publication=release_update_publication,
                    trusted_predecessor=trusted_predecessor,
                    expected_predecessor_trust_sha256=(
                        expected_predecessor_trust_sha256
                    ),
                )
            )
            if (
                validated_prepared["transaction_sha256"]
                != expected_transaction_sha256
            ):
                raise unit_rotation.UnitInputRotationError(
                    "unit_input_rotation_phase_request_invalid"
                )
        if action in {
            "finalize-release-unit-inputs",
            "abort-release-unit-inputs",
        }:
            if not isinstance(preauthorization_receipt, Mapping):
                raise unit_rotation.UnitInputRotationError(
                    "unit_input_rotation_phase_request_invalid"
                )
            validated_preauthorization = (
                unit_rotation.validate_release_preauthorization_receipt(
                    preauthorization_receipt,
                    unit_input_publication=unit_input_publication,
                    release_update_publication=release_update_publication,
                    trusted_predecessor=trusted_predecessor,
                    expected_predecessor_trust_sha256=(
                        expected_predecessor_trust_sha256
                    ),
                    prepared_receipt=validated_prepared,
                )
            )
    except unit_rotation.UnitInputRotationError as exc:
        raise OwnerCutoverError(
            "owner_cutover_release_unit_input_phase_invalid"
        ) from exc
    request: dict[str, Any] = {
        "schema": unit_rotation.RELEASE_PHASE_REQUEST_SCHEMA,
        "action": action,
        "owner_release_revision": owner_release_revision,
        "remote_stager_revision": remote_stager_revision,
        "unit_input_publication": unit_input_publication,
        "release_update_publication": release_update_publication,
        "trusted_predecessor": trusted_predecessor,
        "expected_predecessor_trust_sha256": (
            expected_predecessor_trust_sha256
        ),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    if action != "prepare-release-unit-inputs":
        request.update({
            "prepared_receipt": validated_prepared,
            "expected_transaction_sha256": expected_transaction_sha256,
        })
    if action in {
        "finalize-release-unit-inputs",
        "abort-release-unit-inputs",
    }:
        request["preauthorization_receipt"] = (
            validated_preauthorization
        )
    request["request_sha256"] = _sha(_canonical(request))
    try:
        validated_request = (
            unit_rotation.validate_release_unit_input_phase_request(
                action,
                request,
            )
        )
    except unit_rotation.UnitInputRotationError as exc:
        raise OwnerCutoverError(
            "owner_cutover_release_unit_input_phase_invalid"
        ) from exc
    if validated_request != request:
        raise OwnerCutoverError(
            "owner_cutover_release_unit_input_phase_invalid"
        )
    return copy.deepcopy(dict(validated_request))


def run_release_unit_input_phase(
    *,
    action: str,
    owner_release_revision: str,
    remote_stager_revision: str,
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    transport: Any,
    prepared_receipt: Mapping[str, Any] | None = None,
    preauthorization_receipt: Mapping[str, Any] | None = None,
    expected_transaction_sha256: str | None = None,
) -> Mapping[str, Any]:
    """Run one split release-authority phase over the fixed IAP edge."""

    if (
        action not in {
            "prepare-release-unit-inputs",
            "preauthorize-release-unit-inputs",
        }
        or not callable(getattr(transport, "invoke", None))
    ):
        raise OwnerCutoverError(
            "owner_cutover_release_unit_input_phase_invalid"
        )
    request = build_release_unit_input_phase_request(
        action=action,
        owner_release_revision=owner_release_revision,
        remote_stager_revision=remote_stager_revision,
        unit_input_publication=unit_input_publication,
        release_update_publication=release_update_publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        prepared_receipt=prepared_receipt,
        preauthorization_receipt=preauthorization_receipt,
        expected_transaction_sha256=expected_transaction_sha256,
    )
    validated_prepared = request.get("prepared_receipt")
    validated_preauthorization = request.get(
        "preauthorization_receipt"
    )
    result = transport.invoke(
        remote_stager_revision,
        action,
        release_rotation_request=request,
    )
    try:
        validated_result = (
            unit_rotation.validate_release_unit_input_phase_result(
                action,
                request,
                result,
            )
        )
    except unit_rotation.UnitInputRotationError as exc:
        raise OwnerCutoverError(
            "owner_cutover_release_unit_input_phase_invalid"
        ) from exc
    return copy.deepcopy(dict(validated_result))


def _validate_preflight_receipt(
    value: Mapping[str, Any],
    *,
    plan: cutover.CutoverPlan,
) -> Mapping[str, Any]:
    try:
        return cutover._require_database_receipt(
            value,
            schema="muncho-production-legacy-cutover-preflight.v1",
            plan=plan,
            artifact_name="database_postflight",
        )
    except (TypeError, ValueError, cutover.ProductionCutoverError) as exc:
        raise OwnerCutoverError("owner_cutover_preflight_receipt_invalid") from exc


def _validate_cron_continuity_stage_receipt(
    value: Mapping[str, Any],
    *,
    freeze_plan: cutover.FreezePlan,
) -> Mapping[str, Any]:
    authority = freeze_plan.value["cutover_authority"]
    continuity_plan = authority["cron_continuity_plan"]
    unsigned = {
        name: item for name, item in value.items() if name != "receipt_sha256"
    }
    if continuity_plan.get("schema") != cron_continuity.PLAN_SCHEMA:
        fields = {
            "schema",
            "release_revision",
            "freeze_plan_sha256",
            "continuity_plan_sha256",
            "legacy_noop",
            "artifacts_staged",
            "units_installed",
            "timers_enabled",
            "timers_started",
            "jobs_store_mutated",
            "secret_material_recorded",
            "receipt_sha256",
        }
        if (
            set(value) != fields
            or value.get("schema") != CRON_STAGE_NOOP_SCHEMA
            or value.get("release_revision")
            != freeze_plan.value["release_revision"]
            or value.get("freeze_plan_sha256") != freeze_plan.sha256
            or value.get("continuity_plan_sha256")
            != continuity_plan.get("owner_approved_plan_sha256")
            or value.get("legacy_noop") is not True
            or any(
                value.get(name) is not False
                for name in (
                    "artifacts_staged",
                    "units_installed",
                    "timers_enabled",
                    "timers_started",
                    "jobs_store_mutated",
                    "secret_material_recorded",
                )
            )
            or value.get("receipt_sha256") != _sha(_canonical(unsigned))
        ):
            raise OwnerCutoverError(
                "owner_cutover_cron_continuity_stage_invalid"
            )
        return copy.deepcopy(dict(value))

    fields = {
        "schema",
        "release_revision",
        "plan_relative_path",
        "plan_sha256",
        "replacement_bundle_relative_path",
        "replacement_bundle_sha256",
        "collector_manifest_relative_path",
        "collector_manifest_sha256",
        "cutover_runtime_sha256",
        "cutover_entrypoint_sha256",
        "files",
        "file_count",
        "units_installed",
        "timers_enabled",
        "timers_started",
        "jobs_store_mutated",
        "secret_material_recorded",
        "artifact_index_sha256",
    }
    try:
        collector_package = trusted_cron_collector_rail.validate_package_manifest(
            continuity_plan["trusted_collector_package"],
            revision=freeze_plan.value["release_revision"],
        )
        unit_files = trusted_cron_collector_rail.render_package_unit_files(
            collector_package
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise OwnerCutoverError(
            "owner_cutover_cron_continuity_stage_invalid"
        ) from exc

    def ascii_payload(item: Mapping[str, Any]) -> bytes:
        return json.dumps(
            item,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict") + b"\n"

    expected_rows: dict[str, tuple[str | None, int, bool]] = {
        str(cron_continuity.PLAN_RELATIVE_PATH): (
            _sha(ascii_payload(continuity_plan)),
            0o640,
            False,
        ),
        str(cron_continuity.REPLACEMENT_BUNDLE_RELATIVE_PATH): (
            None,
            0o600,
            True,
        ),
        str(cron_continuity.COLLECTOR_MANIFEST_RELATIVE_PATH): (
            _sha(ascii_payload(collector_package)),
            0o640,
            False,
        ),
    }
    expected_rows.update({
        str(Path("cron/trusted-collector") / relative): (
            _sha(raw),
            0o640,
            False,
        )
        for relative, raw in unit_files.items()
    })
    expected_file_count = 3 + 2 * len(
        trusted_cron_collector_rail.COLLECTOR_SPECS
    )
    files = value.get("files")
    if (
        set(value) != fields
        or value.get("schema") != cron_continuity.ARTIFACT_INDEX_SCHEMA
        or value.get("release_revision")
        != freeze_plan.value["release_revision"]
        or value.get("plan_relative_path")
        != str(cron_continuity.PLAN_RELATIVE_PATH)
        or value.get("plan_sha256") != continuity_plan["plan_sha256"]
        or value.get("replacement_bundle_relative_path")
        != str(cron_continuity.REPLACEMENT_BUNDLE_RELATIVE_PATH)
        or value.get("replacement_bundle_sha256")
        != continuity_plan["replacement_bundle_sha256"]
        or value.get("collector_manifest_relative_path")
        != str(cron_continuity.COLLECTOR_MANIFEST_RELATIVE_PATH)
        or value.get("collector_manifest_sha256")
        != collector_package["manifest_sha256"]
        or value.get("cutover_runtime_sha256")
        != continuity_plan["cutover_runtime_sha256"]
        or value.get("cutover_entrypoint_sha256")
        != continuity_plan["cutover_entrypoint_sha256"]
        or not isinstance(files, list)
        or value.get("file_count") != expected_file_count
        or value.get("file_count") != len(files)
        or len(expected_rows) != expected_file_count
        or tuple(
            item.get("relative_path")
            for item in files
            if isinstance(item, Mapping)
        )
        != tuple(sorted(expected_rows))
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"relative_path", "sha256", "mode", "private"}
            or item["relative_path"] not in expected_rows
            or _SHA256.fullmatch(str(item["sha256"])) is None
            or item["mode"] != expected_rows[item["relative_path"]][1]
            or item["private"] is not expected_rows[item["relative_path"]][2]
            or expected_rows[item["relative_path"]][0] is not None
            and item["sha256"] != expected_rows[item["relative_path"]][0]
            for item in files
        )
        or any(
            value.get(name) is not False
            for name in (
                "units_installed",
                "timers_enabled",
                "timers_started",
                "jobs_store_mutated",
                "secret_material_recorded",
            )
        )
        or value.get("artifact_index_sha256")
        != _sha(_canonical({
            name: item
            for name, item in value.items()
            if name != "artifact_index_sha256"
        }))
    ):
        raise OwnerCutoverError(
            "owner_cutover_cron_continuity_stage_invalid"
        )
    return copy.deepcopy(dict(value))


def _validate_convergence_receipt(
    value: Mapping[str, Any],
    *,
    plan: cutover.CutoverPlan,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CONVERGENCE_FIELDS:
        raise OwnerCutoverError("owner_cutover_convergence_receipt_invalid")
    receipt = copy.deepcopy(dict(value))
    unsigned = {
        name: item
        for name, item in receipt.items()
        if name != "receipt_sha256"
    }
    if (
        receipt.get("schema") != CONVERGENCE_SCHEMA
        or receipt.get("release_revision") != plan.value["release_revision"]
        or receipt.get("freeze_plan_sha256")
        != plan.value["freeze_plan_sha256"]
        or receipt.get("cutover_plan_sha256") != plan.sha256
        or receipt.get("caddy_outcome")
        not in {"private_v2_active", "maintenance_active"}
        or any(
            _SHA256.fullmatch(str(receipt.get(name, ""))) is None
            for name in (
                "preflight_receipt_sha256",
                "caddy_prepare_receipt_sha256",
                "maintenance_arm_receipt_sha256",
                "cutover_terminal_receipt_sha256",
                "caddy_terminal_receipt_sha256",
                "legacy_service_retirement_receipt_sha256",
            )
        )
        or receipt.get("control_plane_mutation_performed") is not True
        or receipt.get("source_data_mutation_performed") is not True
        or receipt.get("production_host_mutation_performed") is not True
        or receipt.get("secret_material_recorded") is not False
        or receipt.get("secret_digest_recorded") is not False
        or receipt.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise OwnerCutoverError("owner_cutover_convergence_receipt_invalid")
    return receipt


def _validate_workflow_receipt(
    value: Mapping[str, Any],
    *,
    release_revision: str,
    freeze_plan_sha256: str,
    freeze_approval_sha256: str,
    cutover_plan_sha256: str,
    convergence: Mapping[str, Any],
    gates: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _WORKFLOW_RECEIPT_FIELDS:
        raise OwnerCutoverError("owner_cutover_workflow_receipt_invalid")
    receipt = copy.deepcopy(dict(value))
    unsigned = {
        name: item
        for name, item in receipt.items()
        if name != "receipt_sha256"
    }
    expected_gates = copy.deepcopy(list(gates))
    valid_gates = all(
        isinstance(item, Mapping)
        and set(item) == {"sequence", "stage", "evidence_sha256"}
        and item.get("sequence") == sequence
        and isinstance(item.get("stage"), str)
        and bool(item["stage"])
        and _SHA256.fullmatch(str(item.get("evidence_sha256", "")))
        is not None
        for sequence, item in enumerate(expected_gates)
    )
    bound_hash_fields = (
        "convergence_receipt_sha256",
        "terminal_receipt_sha256",
        "caddy_prepare_receipt_sha256",
        "caddy_terminal_receipt_sha256",
        "legacy_service_retirement_receipt_sha256",
    )
    if (
        receipt.get("schema") != WORKFLOW_RECEIPT_SCHEMA
        or receipt.get("release_revision") != release_revision
        or receipt.get("freeze_plan_sha256") != freeze_plan_sha256
        or receipt.get("freeze_approval_sha256")
        != freeze_approval_sha256
        or receipt.get("cutover_plan_sha256") != cutover_plan_sha256
        or receipt.get("convergence_receipt_sha256")
        != convergence.get("receipt_sha256")
        or receipt.get("terminal_receipt_sha256")
        != convergence.get("cutover_terminal_receipt_sha256")
        or receipt.get("caddy_prepare_receipt_sha256")
        != convergence.get("caddy_prepare_receipt_sha256")
        or receipt.get("caddy_terminal_receipt_sha256")
        != convergence.get("caddy_terminal_receipt_sha256")
        or receipt.get("caddy_outcome") != convergence.get("caddy_outcome")
        or receipt.get("legacy_service_retirement_receipt_sha256")
        != convergence.get("legacy_service_retirement_receipt_sha256")
        or any(
            _SHA256.fullmatch(str(receipt.get(name, ""))) is None
            for name in bound_hash_fields
        )
        or receipt.get("gates") != expected_gates
        or not valid_gates
        or not expected_gates
        or expected_gates[-1].get("stage")
        != "cutover_convergence_accepted"
        or expected_gates[-1].get("evidence_sha256")
        != _sha(_canonical(convergence))
        or receipt.get("private_key_staged") is not False
        or receipt.get("control_plane_mutation_performed") is not True
        or receipt.get("source_data_mutation_performed") is not True
        or receipt.get("production_host_mutation_performed") is not True
        or receipt.get("secret_material_recorded") is not False
        or receipt.get("secret_digest_recorded") is not False
        or receipt.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise OwnerCutoverError("owner_cutover_workflow_receipt_invalid")
    return receipt


def _build_workflow_receipt(
    *,
    release_revision: str,
    freeze_plan_sha256: str,
    freeze_approval_sha256: str,
    cutover_plan_sha256: str,
    convergence: Mapping[str, Any],
    gates: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": WORKFLOW_RECEIPT_SCHEMA,
        "release_revision": release_revision,
        "freeze_plan_sha256": freeze_plan_sha256,
        "freeze_approval_sha256": freeze_approval_sha256,
        "cutover_plan_sha256": cutover_plan_sha256,
        "convergence_receipt_sha256": convergence["receipt_sha256"],
        "terminal_receipt_sha256": convergence[
            "cutover_terminal_receipt_sha256"
        ],
        "caddy_prepare_receipt_sha256": convergence[
            "caddy_prepare_receipt_sha256"
        ],
        "caddy_terminal_receipt_sha256": convergence[
            "caddy_terminal_receipt_sha256"
        ],
        "caddy_outcome": convergence["caddy_outcome"],
        "legacy_service_retirement_receipt_sha256": convergence[
            "legacy_service_retirement_receipt_sha256"
        ],
        "gates": copy.deepcopy(list(gates)),
        "private_key_staged": False,
        "control_plane_mutation_performed": True,
        "source_data_mutation_performed": True,
        "production_host_mutation_performed": True,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return _validate_workflow_receipt(
        {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))},
        release_revision=release_revision,
        freeze_plan_sha256=freeze_plan_sha256,
        freeze_approval_sha256=freeze_approval_sha256,
        cutover_plan_sha256=cutover_plan_sha256,
        convergence=convergence,
        gates=gates,
    )


def _validate_terminal_receipt(
    value: Mapping[str, Any],
    *,
    plan: cutover.CutoverPlan,
) -> Mapping[str, Any]:
    common_fields = {
        "schema",
        "plan_sha256",
        "freeze_plan_sha256",
        "freeze_approval_sha256",
        "approval_sha256",
        "final_tail_receipt_sha256",
        "capability_prerequisite_receipt_sha256",
        "capability_prerequisite_file_sha256",
        "zero_canonical_database_mutation_observed",
        "pre_db_zero_write_observation_sha256",
        "capability_topology_identity_sha256",
        "database_apply_receipt_sha256",
        "host_apply_receipt_sha256",
        "host_boot_commit_receipt_sha256",
        "activation_commit_intent_receipt_sha256",
        "alias_projection_package_sha256",
        "alias_projection_activation_authority_sha256",
        "alias_projection_activation_receipt_sha256",
        "database_postflight_receipt_sha256",
        "gateway_observation_sha256",
        "writer_observation_sha256",
        "connector_observation_sha256",
        "direct_discord_disabled",
        "discord_dm_allowed",
        "rollback_used",
        "secret_material_recorded",
        "completed_at_unix",
        "receipt_sha256",
    }
    canary_fields = common_fields | {
        "isolated_canary_goal_continuation_terminal_sha256",
        "isolated_canary_workspace_gateway_receipt_sha256",
        "isolation_equivalence_projection_sha256",
    }
    direct_mvp_fields = common_fields | {
        "release_validation_mode",
        "release_validation_authority_sha256",
        "isolated_canary_proof_present",
        "owner_approved_direct_mvp_no_canary",
        "live_production_prerequisite_validated",
    }
    unsigned = {
        name: item for name, item in value.items() if name != "receipt_sha256"
    }
    validation_mode, validation_authority = (
        cutover._validate_release_validation_authority(
            plan.value["freeze_plan"]["cutover_authority"][
                "isolated_canary_goal_prerequisite"
            ],
            revision=plan.value["release_revision"],
        )
    )
    common_invalid = (
        value.get("plan_sha256") != plan.sha256
        or value.get("freeze_plan_sha256") != plan.value["freeze_plan_sha256"]
        or value.get("freeze_approval_sha256")
        != plan.value["freeze_approval_sha256"]
        or value.get("final_tail_receipt_sha256")
        != plan.value["final_tail_receipt_sha256"]
        or value.get("direct_discord_disabled") is not True
        or value.get("discord_dm_allowed") is not False
        or value.get("rollback_used") is not False
        or value.get("zero_canonical_database_mutation_observed") is not True
        or value.get("secret_material_recorded") is not False
        or any(
            _SHA256.fullmatch(str(value.get(field))) is None
            for field in set(value)
            if field.endswith("_sha256")
        )
        or type(value.get("completed_at_unix")) is not int
        or value.get("completed_at_unix", 0) <= 0
        or _SHA256.fullmatch(str(value.get("receipt_sha256"))) is None
        or value.get("receipt_sha256") != _sha(_canonical(unsigned))
    )
    if common_invalid:
        raise OwnerCutoverError("owner_cutover_terminal_receipt_invalid")
    if value.get("schema") == cutover.TERMINAL_SCHEMA:
        if validation_mode != "release_bound_isolated_canary":
            raise OwnerCutoverError("owner_cutover_terminal_receipt_invalid")
        expected_equivalence = cutover._build_production_isolation_equivalence(
            plan=plan,
            evidence=validation_authority,
        )
        if (
            set(value) != canary_fields
            or value.get(
                "isolated_canary_goal_continuation_terminal_sha256"
            )
            != validation_authority["goal_continuation_terminal_sha256"]
            or value.get(
                "isolated_canary_workspace_gateway_receipt_sha256"
            )
            != validation_authority["workspace_gateway_receipt_sha256"]
            or value.get("isolation_equivalence_projection_sha256")
            != expected_equivalence["projection_sha256"]
        ):
            raise OwnerCutoverError(
                "owner_cutover_terminal_receipt_invalid"
            )
    elif value.get("schema") == cutover.DIRECT_MVP_TERMINAL_SCHEMA:
        if (
            set(value) != direct_mvp_fields
            or validation_mode != cutover.OWNER_DIRECT_MVP_VALIDATION_MODE
            or value.get("release_validation_mode") != validation_mode
            or value.get("release_validation_authority_sha256")
            != validation_authority["waiver_sha256"]
            or value.get("isolated_canary_proof_present") is not False
            or value.get("owner_approved_direct_mvp_no_canary") is not True
            or value.get("live_production_prerequisite_validated") is not True
        ):
            raise OwnerCutoverError(
                "owner_cutover_terminal_receipt_invalid"
            )
    else:
        raise OwnerCutoverError("owner_cutover_terminal_receipt_invalid")
    return copy.deepcopy(dict(value))


def _validate_cutover_passkey_request(
    value: Any,
    *,
    release_revision: str,
    freeze_plan_sha256: str,
    freeze_publication_sha256: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OwnerCutoverError("owner_cutover_passkey_request_invalid")
    request = copy.deepcopy(dict(value))
    request_id = request.get("request_id")
    if (
        set(request) != _CUTOVER_PASSKEY_REQUEST_FIELDS
        or not isinstance(request_id, str)
        or _CUTOVER_REQUEST_ID.fullmatch(request_id) is None
        or _SHA256.fullmatch(
            str(request.get("action_envelope_sha256"))
        ) is None
        or _SHA256.fullmatch(
            str(request.get("challenge_record_sha256"))
        ) is None
        or type(request.get("expires_at_unix")) is not int
        or request["expires_at_unix"] <= 0
        or request.get("release_sha") != release_revision
        or request.get("plan_sha256") != freeze_plan_sha256
        or request.get("freeze_publication_sha256")
        != freeze_publication_sha256
        or _SHA256.fullmatch(str(request.get("transaction_id"))) is None
        or _SHA256.fullmatch(
            str(request.get("action_payload_sha256"))
        ) is None
        or request.get("approval_url")
        != (
            f"{cutover_passkey.protocol.PRODUCTION_ORIGIN}/approve/"
            f"{request_id}"
        )
        or request.get("passkey_only") is not True
        or request.get("single_use") is not True
        or request.get("control_plane_mutation_performed") is not True
        or request.get("source_data_mutation_performed") is not False
        or request.get("production_host_mutation_performed") is not False
    ):
        raise OwnerCutoverError("owner_cutover_passkey_request_invalid")
    return request


def _build_bridge_bootstrap_input(
    *,
    release_revision: str,
    freeze_plan_sha256: str,
    freeze_approval_sha256: str,
    freeze_publication_sha256: str,
    passkey_request: Mapping[str, Any],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": BRIDGE_BOOTSTRAP_INPUT_SCHEMA,
        "release_revision": release_revision,
        "freeze_plan_sha256": freeze_plan_sha256,
        "freeze_approval_sha256": freeze_approval_sha256,
        "freeze_publication_sha256": freeze_publication_sha256,
        "v2_request_id": passkey_request["request_id"],
        "v2_expires_at_unix": passkey_request["expires_at_unix"],
        "v2_transaction_id": passkey_request["transaction_id"],
        "v2_approval_url_sha256": _sha(
            str(passkey_request["approval_url"]).encode(
                "ascii", errors="strict"
            )
        ),
        "v2_action_payload_sha256": passkey_request[
            "action_payload_sha256"
        ],
    }
    return {
        **unsigned,
        "document_sha256": _sha(_canonical(unsigned)),
    }


def _validate_bridge_bootstrap_input(value: Any) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _BRIDGE_BOOTSTRAP_INPUT_FIELDS
    ):
        raise OwnerCutoverError("owner_cutover_bridge_bootstrap_invalid")
    document = copy.deepcopy(dict(value))
    unsigned = {
        name: item for name, item in document.items()
        if name != "document_sha256"
    }
    request_id = document.get("v2_request_id")
    approval_url = (
        f"{cutover_passkey.protocol.PRODUCTION_ORIGIN}/approve/{request_id}"
    )
    if (
        document.get("schema") != BRIDGE_BOOTSTRAP_INPUT_SCHEMA
        or package.REVISION.fullmatch(
            str(document.get("release_revision", ""))
        )
        is None
        or not isinstance(request_id, str)
        or _CUTOVER_REQUEST_ID.fullmatch(request_id) is None
        or type(document.get("v2_expires_at_unix")) is not int
        or document["v2_expires_at_unix"] <= 0
        or any(
            _SHA256.fullmatch(str(document.get(name, ""))) is None
            for name in (
                "freeze_plan_sha256",
                "freeze_approval_sha256",
                "freeze_publication_sha256",
                "v2_transaction_id",
                "v2_approval_url_sha256",
                "v2_action_payload_sha256",
                "document_sha256",
            )
        )
        or document["v2_approval_url_sha256"]
        != _sha(approval_url.encode("ascii", errors="strict"))
        or document["document_sha256"] != _sha(_canonical(unsigned))
    ):
        raise OwnerCutoverError("owner_cutover_bridge_bootstrap_invalid")
    return document


def _validate_bridge_request(
    value: Any,
    *,
    document: Mapping[str, Any],
) -> Mapping[str, Any]:
    bootstrap = _validate_bridge_bootstrap_input(document)
    if not isinstance(value, Mapping) or set(value) != _BRIDGE_REQUEST_FIELDS:
        raise OwnerCutoverError("owner_cutover_bridge_request_invalid")
    request = copy.deepcopy(dict(value))
    unsigned = {
        name: item for name, item in request.items()
        if name != "receipt_sha256"
    }
    legacy_request_id = request.get("legacy_passkey_request_id")
    binding = {
        "release_revision": "release_revision",
        "freeze_plan_sha256": "freeze_plan_sha256",
        "freeze_approval_sha256": "freeze_approval_sha256",
        "freeze_publication_sha256": "freeze_publication_sha256",
        "v2_request_id": "v2_request_id",
        "v2_expires_at_unix": "v2_expires_at_unix",
        "v2_transaction_id": "v2_transaction_id",
        "v2_approval_url_sha256": "v2_approval_url_sha256",
        "v2_action_payload_sha256": "v2_action_payload_sha256",
    }
    if (
        request.get("schema") != BRIDGE_REQUEST_SCHEMA
        or any(
            request.get(left) != bootstrap[right]
            for left, right in binding.items()
        )
        or request.get("bootstrap_input_sha256")
        != bootstrap["document_sha256"]
        or not isinstance(legacy_request_id, str)
        or _LEGACY_REQUEST_ID.fullmatch(legacy_request_id) is None
        or request.get("legacy_approval_url")
        != (
            f"{cutover_passkey.protocol.PRODUCTION_ORIGIN}/approve/"
            f"{legacy_request_id}"
        )
        or any(
            _SHA256.fullmatch(str(request.get(name, ""))) is None
            for name in (
                "legacy_passkey_request_sha256",
                "bridge_action_sha256",
                "route_contract_sha256",
                "original_caddy_sha256",
                "approval_bridge_template_sha256",
                "approval_bridge_caddy_sha256",
                "receipt_sha256",
            )
        )
        or request.get("default_local_v1_route_preserved") is not True
        or request.get("control_plane_mutation_performed") is not True
        or request.get("source_data_mutation_performed") is not False
        or request.get("production_host_mutation_performed") is not True
        or request.get("caller_selected_input_accepted") is not False
        or request.get("secret_material_recorded") is not False
        or request.get("secret_digest_recorded") is not False
        or type(request.get("requested_at_unix")) is not int
        or request["requested_at_unix"] <= 0
        or request["requested_at_unix"]
        + MINIMUM_V2_APPROVAL_MARGIN_SECONDS
        >= request["v2_expires_at_unix"]
        or request["receipt_sha256"] != _sha(_canonical(unsigned))
    ):
        raise OwnerCutoverError("owner_cutover_bridge_request_invalid")
    return request


def _validate_bridge_receipt(
    value: Any,
    *,
    document: Mapping[str, Any],
    bridge_request: Mapping[str, Any],
) -> Mapping[str, Any]:
    bootstrap = _validate_bridge_bootstrap_input(document)
    requested = _validate_bridge_request(
        bridge_request,
        document=bootstrap,
    )
    if not isinstance(value, Mapping) or set(value) != _BRIDGE_RECEIPT_FIELDS:
        raise OwnerCutoverError("owner_cutover_bridge_receipt_invalid")
    receipt = copy.deepcopy(dict(value))
    unsigned = {
        name: item for name, item in receipt.items()
        if name != "receipt_sha256"
    }
    binding_names = (
        "release_revision",
        "freeze_plan_sha256",
        "freeze_approval_sha256",
        "freeze_publication_sha256",
        "v2_request_id",
        "v2_expires_at_unix",
        "v2_transaction_id",
        "v2_approval_url_sha256",
        "v2_action_payload_sha256",
    )
    if (
        receipt.get("schema") != BRIDGE_RECEIPT_SCHEMA
        or any(receipt.get(name) != bootstrap[name] for name in binding_names)
        or receipt.get("bootstrap_input_sha256")
        != bootstrap["document_sha256"]
        or receipt.get("bridge_request_receipt_sha256")
        != requested["receipt_sha256"]
        or receipt.get("legacy_passkey_request_id")
        != requested["legacy_passkey_request_id"]
        or receipt.get("legacy_passkey_request_sha256")
        != requested["legacy_passkey_request_sha256"]
        or receipt.get("bridge_action_sha256")
        != requested["bridge_action_sha256"]
        or receipt.get("route_contract_sha256")
        != requested["route_contract_sha256"]
        or receipt.get("original_caddy_sha256")
        != requested["original_caddy_sha256"]
        or receipt.get("approval_bridge_caddy_sha256")
        != requested["approval_bridge_caddy_sha256"]
        or not isinstance(receipt.get("legacy_passkey_grant_id"), str)
        or re.fullmatch(
            r"[A-Za-z0-9_-]{16,128}",
            receipt["legacy_passkey_grant_id"],
        )
        is None
        or any(
            _SHA256.fullmatch(str(receipt.get(name, ""))) is None
            for name in (
                "legacy_passkey_grant_sha256",
                "legacy_passkey_consumed_grant_sha256",
                "legacy_passkey_consume_entry_sha256",
                "legacy_service_active_before_sha256",
                "legacy_service_inactive_sha256",
                "legacy_service_active_after_sha256",
                "legacy_service_local_health_sha256",
                "active_route_projection_sha256",
                "receipt_sha256",
            )
        )
        or receipt.get("default_local_v1_route_preserved") is not True
        or receipt.get("exact_v2_approval_routes_only") is not True
        or receipt.get("caddy_validated") is not True
        or receipt.get("caddy_reloaded") is not True
        or receipt.get("caddy_readback_verified") is not True
        or receipt.get("rollback_mode") != "pre_migration_exact_bytes"
        or receipt.get("control_plane_mutation_performed") is not True
        or receipt.get("source_data_mutation_performed") is not False
        or receipt.get("production_host_mutation_performed") is not True
        or receipt.get("caller_selected_input_accepted") is not False
        or receipt.get("secret_material_recorded") is not False
        or receipt.get("secret_digest_recorded") is not False
        or type(receipt.get("activated_at_unix")) is not int
        or receipt["activated_at_unix"] <= 0
        or not requested["requested_at_unix"]
        <= receipt["activated_at_unix"]
        or receipt["activated_at_unix"]
        + MINIMUM_V2_APPROVAL_MARGIN_SECONDS
        >= receipt["v2_expires_at_unix"]
        or receipt["receipt_sha256"] != _sha(_canonical(unsigned))
    ):
        raise OwnerCutoverError("owner_cutover_bridge_receipt_invalid")
    return receipt


def execute_production_cutover_workflow(
    *,
    release_revision: str,
    owner_identity: Any,
    owner_subject_sha256: str,
    private_key: Ed25519PrivateKey,
    isolated_canary_goal_prerequisite: Mapping[str, Any],
    legacy_predecessor_revision: str,
    truth_mode: str,
    accepted_event_receipts: list[Mapping[str, Any]] | None = None,
    passkey_proof: Mapping[str, Any] | None = None,
    passkey_boundary: Any | None = None,
    prepare_only: bool = False,
    transport_factory: Any = ProductionCutoverTransport,
    database_recovery_gate_runner: Any = database_recovery.run_for_owner,
    now_unix: int | None = None,
    clock: Callable[[], float] = time.time,
    revision_ancestry_checker: Callable[[str, str], bool] | None = None,
) -> Mapping[str, Any]:
    """Execute the fixed production cutover state machine.

    Before the owner signs the FreezePlan, the workflow collects the two
    inert fixed host artifacts are staged before the read-only authorities and
    database-recovery gate.  No caller-authored host plan is accepted.  The
    recovery gate's only mutations are a retained on-demand backup and a
    release-bound scratch instance that is deleted after a durable read-only
    probe.  Any failure after freeze publication and before a cutover plan is
    confirmed staged invokes the exact ``abort-freeze`` recovery action.
    """

    if not callable(clock):
        raise OwnerCutoverError("owner_cutover_workflow_input_invalid")

    def gate_now() -> int:
        return int(clock()) if now_unix is None else now_unix

    if revision_ancestry_checker is None:
        def revision_ancestry_checker(predecessor: str, target: str) -> bool:
            try:
                completed = subprocess.run(
                    (
                        "/usr/bin/git",
                        "merge-base",
                        "--is-ancestor",
                        predecessor,
                        target,
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=Path(__file__).resolve().parents[2],
                    env={"LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return completed.returncode == 0

    if (
        package.REVISION.fullmatch(release_revision or "") is None
        or legacy_predecessor_revision != LEGACY_F5_PREDECESSOR_REVISION
        or release_revision == legacy_predecessor_revision
        or release_revision[:12] == legacy_predecessor_revision[:12]
        or truth_mode != "start_new_truth_epoch"
        or accepted_event_receipts is not None
        or not callable(revision_ancestry_checker)
        or not revision_ancestry_checker(
            legacy_predecessor_revision, release_revision
        )
        or _SHA256.fullmatch(owner_subject_sha256 or "") is None
        or not callable(transport_factory)
        or not callable(database_recovery_gate_runner)
    ):
        raise OwnerCutoverError("owner_cutover_workflow_input_invalid")
    runtime_attestation = _active_owner_runtime_attestation(
        release_revision
    )
    transport = transport_factory(owner_identity)
    if not isinstance(transport, ProductionCutoverTransport) and not callable(
        getattr(transport, "invoke", None)
    ):
        raise OwnerCutoverError("owner_cutover_transport_invalid")

    gates: list[Mapping[str, Any]] = []

    def record(stage: str, value: Mapping[str, Any]) -> None:
        gates.append({
            "sequence": len(gates),
            "stage": stage,
            "evidence_sha256": _sha(_canonical(value)),
        })

    staged_host = transport.invoke(release_revision, "stage-host-artifacts")
    if (
        not isinstance(staged_host, Mapping)
        or staged_host.get("schema")
        != "muncho-production-cutover-fixed-host-staging.v1"
        or staged_host.get("release_revision") != release_revision
        or staged_host.get("staged_file_count")
        != len(package.HOST_ARTIFACT_TARGETS)
        or staged_host.get("secret_material_recorded") is not False
        or staged_host.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(str(staged_host.get("receipt_sha256"))) is None
    ):
        raise OwnerCutoverError("owner_cutover_host_staging_invalid")
    record("fixed_host_artifacts_staged", staged_host)
    initial = validate_initial_collector_receipt(
        transport.invoke(release_revision, "collect-initial"),
        release_revision=release_revision,
        now_unix=gate_now(),
    )
    record("initial_read_only_collected", initial)
    host_authority_plan = transport.invoke(
        release_revision,
        "collect-host-plan",
        initial_receipt=initial,
    )
    if (
        not isinstance(host_authority_plan, Mapping)
        or set(host_authority_plan) != _HOST_AUTHORITY_PLAN_FIELDS
        or host_authority_plan["cron_continuity_plan"]
        != initial["cron_continuity_plan"]
    ):
        raise OwnerCutoverError("owner_cutover_fixed_host_plan_invalid")
    record("fixed_host_plan_collected", host_authority_plan)
    authority_request = host_authority.build_host_authority_request(
        initial_collector_receipt=initial,
        release_manifest_sha256=str(
            host_authority_plan["release_manifest_sha256"]
        ),
        gateway_target_identity=host_authority_plan[
            "gateway_target_identity"
        ],
        writer_target_identity=host_authority_plan["writer_target_identity"],
        connector_target_identity=host_authority_plan[
            "connector_target_identity"
        ],
        host_transition=host_authority_plan["host_transition"],
        capability_topology=host_authority_plan["capability_topology"],
        cron_continuity_plan=host_authority_plan["cron_continuity_plan"],
    )
    host_receipt = host_authority.validate_host_authority_receipt(
        transport.invoke(
            release_revision,
            "collect-authority",
            authority_request=authority_request,
        ),
        host_authority_request=authority_request,
        initial_collector_receipt=initial,
        release_revision=release_revision,
        now_unix=gate_now(),
    )
    record("host_authority_read_only_collected", host_receipt)
    full_authority = host_authority.compose_full_authority_receipt(
        initial_collector_receipt=initial,
        host_authority_request=authority_request,
        host_authority_receipt=host_receipt,
        release_revision=release_revision,
        now_unix=gate_now(),
    )
    record("full_authority_composed", full_authority)
    try:
        recovery_receipt = database_recovery.validate_receipt_for_freeze(
            database_recovery_gate_runner(
                release_revision,
                owner_identity,
                owner_subject_sha256,
            ),
            release_revision=release_revision,
            now_unix=gate_now(),
        )
    except database_recovery.ProductionDatabaseRecoveryError as exc:
        raise OwnerCutoverError(
            "owner_cutover_database_recovery_failed"
        ) from exc
    record("database_recovery_validated", recovery_receipt)
    freeze, freeze_approval, freeze_publication = author_freeze(
        collector_receipt=full_authority,
        release_revision=release_revision,
        owner_subject_sha256=owner_subject_sha256,
        private_key=private_key,
        owner_runtime_attestation=runtime_attestation,
        isolated_canary_goal_prerequisite=(
            isolated_canary_goal_prerequisite
        ),
        database_recovery_receipt=recovery_receipt,
        truth_mode=truth_mode,
        accepted_event_receipts=accepted_event_receipts,
        now_unix=gate_now(),
    )
    # The private key is consumed only above and never passed to transport.
    record("freeze_owner_signed", {
        "plan_sha256": freeze.sha256,
        "approval_sha256": freeze_approval["approval_sha256"],
        "publication_sha256": freeze_publication["publication_sha256"],
    })
    if prepare_only:
        if (
            passkey_proof is not None
            or not callable(getattr(passkey_boundary, "request", None))
        ):
            raise OwnerCutoverError(
                "owner_cutover_passkey_prepare_boundary_invalid"
            )
        request = _validate_cutover_passkey_request(
            passkey_boundary.request(freeze_publication),
            release_revision=release_revision,
            freeze_plan_sha256=freeze.sha256,
            freeze_publication_sha256=freeze_publication[
                "publication_sha256"
            ],
        )
        record("single_use_passkey_requested", request)
        bridge_bootstrap_input = _build_bridge_bootstrap_input(
            release_revision=release_revision,
            freeze_plan_sha256=freeze.sha256,
            freeze_approval_sha256=freeze_approval[
                "approval_sha256"
            ],
            freeze_publication_sha256=freeze_publication[
                "publication_sha256"
            ],
            passkey_request=request,
        )
        unsigned_workspace = {
            "schema": PREPARED_WORKSPACE_SCHEMA,
            "state": "awaiting_bridge_bootstrap",
            "release_revision": release_revision,
            "legacy_predecessor_revision": legacy_predecessor_revision,
            "owner_subject_sha256": owner_subject_sha256,
            "freeze_plan": freeze.to_mapping(),
            "freeze_approval": freeze_approval,
            "freeze_publication": freeze_publication,
            "passkey_request": copy.deepcopy(dict(request)),
            "bridge_bootstrap_input": bridge_bootstrap_input,
            "bridge_request": None,
            "bridge_receipt": None,
            "advertised_approval_url": None,
            "passkey_consumption": None,
            "claim_frame": None,
            "final_tail_receipt": None,
            "stopped_collector_receipt": None,
            "cron_continuity_stage_receipt": None,
            "cutover_plan": None,
            "cutover_publication": None,
            "cutover_stage_receipt": None,
            "gates": gates,
            "private_key_staged": False,
            "control_plane_mutation_performed": True,
            "source_data_mutation_performed": False,
            "production_host_mutation_performed": False,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
        return {
            **unsigned_workspace,
            "workspace_sha256": _sha(_canonical(unsigned_workspace)),
        }
    if passkey_proof is None:
        raise OwnerCutoverError(
            "owner_cutover_passkey_proof_required_before_first_write"
        )
    try:
        claim_frame = cutover_passkey.build_claim_frame(
            publication=freeze_publication,
            passkey_proof=passkey_proof,
            now_unix=gate_now(),
        )
    except cutover_passkey.ProductionCutoverPasskeyError:
        raise OwnerCutoverError(
            "owner_cutover_passkey_proof_invalid"
        ) from None
    record("single_use_passkey_consumed", {
        "proof_sha256": passkey_proof["proof_sha256"],
        "authorization_receipt_sha256": passkey_proof[
            "authorization_receipt"
        ]["receipt_sha256"],
        "claim_sha256": claim_frame["claim_sha256"],
    })

    freeze_staged = False
    cutover_staged = False
    try:
        stage_receipt = _validate_publication_stage_receipt(
            transport.invoke(
                release_revision,
                "stage-publication",
                publication=claim_frame,
            ),
            publication=freeze_publication,
            expected_file_count=2,
        )
        freeze_staged = True
        record("freeze_authority_staged", stage_receipt)
        tail = cutover.FinalTailReceipt.from_mapping(
            transport.invoke(release_revision, "capture-final-tail"),
            plan=freeze,
        )
        if tail.value["approval_sha256"] != freeze_approval["approval_sha256"]:
            raise OwnerCutoverError("owner_cutover_final_tail_authority_mismatch")
        record("final_tail_captured", tail.to_mapping())
        stopped = validate_stopped_collector_receipt(
            transport.invoke(release_revision, "collect-stopped"),
            freeze_plan=freeze.to_mapping(),
            freeze_approval=freeze_approval,
            now_unix=gate_now(),
        )
        record("stopped_services_collected", stopped)
        cron_stage = _validate_cron_continuity_stage_receipt(
            transport.invoke(release_revision, "stage-cron-continuity"),
            freeze_plan=freeze,
        )
        record("cron_continuity_stage_accepted", cron_stage)
        cutover_plan, cutover_publication = author_cutover(
            freeze_plan=freeze.to_mapping(),
            freeze_approval=freeze_approval,
            final_tail_receipt=tail.to_mapping(),
            gateway_stopped=stopped["gateway_stopped"],
            writer_stopped=stopped["writer_stopped"],
            connector_stopped=stopped["connector_stopped"],
            now_unix=gate_now(),
        )
        record("cutover_plan_composed", {
            "plan_sha256": cutover_plan.sha256,
            "publication_sha256": cutover_publication["publication_sha256"],
        })
        cutover_stage_receipt = _validate_publication_stage_receipt(
            transport.invoke(
                release_revision,
                "stage-publication",
                publication=cutover_publication,
            ),
            publication=cutover_publication,
            expected_file_count=1,
        )
        cutover_staged = True
        record("cutover_plan_staged", cutover_stage_receipt)
        convergence = _validate_convergence_receipt(
            transport.invoke(release_revision, "converge-cutover"),
            plan=cutover_plan,
        )
        record("cutover_convergence_accepted", convergence)
    except BaseException as primary:
        if freeze_staged and not cutover_staged:
            try:
                aborted = transport.invoke(release_revision, "abort-freeze")
                if (
                    cutover._validate_freeze_abort_receipt(
                        aborted,
                        plan=freeze,
                    )["approval_sha256"]
                    != freeze_approval["approval_sha256"]
                ):
                    raise OwnerCutoverError(
                        "owner_cutover_freeze_abort_receipt_invalid"
                    )
            except BaseException as abort_error:
                raise BaseExceptionGroup(
                    "production cutover failed and freeze abort was incomplete",
                    [primary, abort_error],
                ) from None
        raise

    return _build_workflow_receipt(
        release_revision=release_revision,
        freeze_plan_sha256=freeze.sha256,
        freeze_approval_sha256=freeze_approval["approval_sha256"],
        cutover_plan_sha256=cutover_plan.sha256,
        convergence=convergence,
        gates=gates,
    )


_PREPARED_WORKSPACE_FIELDS = frozenset({
    "schema",
    "state",
    "release_revision",
    "legacy_predecessor_revision",
    "owner_subject_sha256",
    "freeze_plan",
    "freeze_approval",
    "freeze_publication",
    "passkey_request",
    "bridge_bootstrap_input",
    "bridge_request",
    "bridge_receipt",
    "advertised_approval_url",
    "passkey_consumption",
    "claim_frame",
    "final_tail_receipt",
    "stopped_collector_receipt",
    "cron_continuity_stage_receipt",
    "cutover_plan",
    "cutover_publication",
    "cutover_stage_receipt",
    "gates",
    "private_key_staged",
    "control_plane_mutation_performed",
    "source_data_mutation_performed",
    "production_host_mutation_performed",
    "secret_material_recorded",
    "secret_digest_recorded",
    "workspace_sha256",
})


def resume_prepared_production_cutover_workflow(
    *,
    workspace: Mapping[str, Any],
    owner_identity: Any,
    passkey_boundary: Any,
    bridge_bootstrap: Any | None = None,
    transport_factory: Any = ProductionCutoverTransport,
    now_unix: int | None = None,
    clock: Callable[[], float] = time.time,
) -> Mapping[str, Any]:
    """Advance one durable owner-gate phase for the exact workspace."""

    if (
        not isinstance(workspace, Mapping)
        or set(workspace) != _PREPARED_WORKSPACE_FIELDS
        or not callable(clock)
        or not callable(transport_factory)
    ):
        raise OwnerCutoverError("owner_cutover_workspace_invalid")
    unsigned_workspace = {
        name: item for name, item in workspace.items()
        if name != "workspace_sha256"
    }
    if (
        workspace.get("schema") != PREPARED_WORKSPACE_SCHEMA
        or workspace.get("state") not in {
            "awaiting_bridge_bootstrap",
            "awaiting_bridge_passkey",
            "awaiting_cutover_passkey",
            "passkey_claim_recorded",
            "cutover_staged",
        }
        or workspace.get("workspace_sha256")
        != _sha(_canonical(unsigned_workspace))
        or package.REVISION.fullmatch(
            str(workspace.get("release_revision"))
        ) is None
        or workspace.get("legacy_predecessor_revision")
        != LEGACY_F5_PREDECESSOR_REVISION
        or workspace.get("release_revision")
        == workspace.get("legacy_predecessor_revision")
        or str(workspace.get("release_revision"))[:12]
        == str(workspace.get("legacy_predecessor_revision"))[:12]
        or _SHA256.fullmatch(
            str(workspace.get("owner_subject_sha256"))
        ) is None
        or workspace.get("private_key_staged") is not False
        or workspace.get("secret_material_recorded") is not False
        or workspace.get("secret_digest_recorded") is not False
        or not isinstance(workspace.get("gates"), list)
        or not isinstance(workspace.get("passkey_request"), Mapping)
        or not isinstance(
            workspace.get("bridge_bootstrap_input"), Mapping
        )
        or workspace.get("control_plane_mutation_performed") is not True
        or workspace.get("source_data_mutation_performed") is not False
    ):
        raise OwnerCutoverError("owner_cutover_workspace_invalid")
    state = str(workspace["state"])
    expected_host_mutation = state != "awaiting_bridge_bootstrap"
    durable_cutover_fields = (
        "final_tail_receipt",
        "stopped_collector_receipt",
        "cron_continuity_stage_receipt",
        "cutover_plan",
        "cutover_publication",
        "cutover_stage_receipt",
    )
    has_recorded_claim = state in {
        "passkey_claim_recorded",
        "cutover_staged",
    }
    if (
        workspace.get("production_host_mutation_performed")
        is not expected_host_mutation
        or (
            has_recorded_claim
            and (
                not isinstance(workspace.get("passkey_consumption"), Mapping)
                or not isinstance(workspace.get("claim_frame"), Mapping)
            )
        )
        or (
            not has_recorded_claim
            and (
                workspace.get("passkey_consumption") is not None
                or workspace.get("claim_frame") is not None
            )
        )
        or (
            state == "cutover_staged"
            and any(
                not isinstance(workspace.get(name), Mapping)
                for name in durable_cutover_fields
            )
        )
        or (
            state != "cutover_staged"
            and any(
                workspace.get(name) is not None
                for name in durable_cutover_fields
            )
        )
    ):
        raise OwnerCutoverError("owner_cutover_workspace_invalid")

    def gate_now() -> int:
        return int(clock()) if now_unix is None else now_unix

    release_revision = str(workspace["release_revision"])
    try:
        freeze = cutover.FreezePlan.from_mapping(workspace["freeze_plan"])
        approval_issued_at = workspace["freeze_approval"][
            "issued_at_unix"
        ]
        approval = cutover.CutoverApproval.from_mapping(
            workspace["freeze_approval"],
            plan=freeze,
            now_unix=approval_issued_at,
        ).value
        freeze_publication = copy.deepcopy(
            dict(workspace["freeze_publication"])
        )
        rebuilt_publication = build_publication(
            action="freeze-authority",
            release_revision=release_revision,
            documents={
                "plan": freeze.to_mapping(),
                "approval": approval,
            },
            now_unix=approval_issued_at,
        )
    except (
        KeyError,
        PermissionError,
        PublicStagingError,
        TypeError,
        ValueError,
    ):
        raise OwnerCutoverError("owner_cutover_workspace_invalid") from None
    if (
        freeze.value["release_revision"] != release_revision
        or freeze.value["cutover_authority"]["legacy_truth_decision"][
            "mode"
        ]
        != "start_new_truth_epoch"
        or freeze.value["cutover_authority"]["legacy_truth_decision"][
            "accepted_event_receipts"
        ]
        != []
        or freeze.value["cutover_authority"]["legacy_truth_decision"][
            "accepted_event_ids"
        ]
        != []
        or freeze_publication != rebuilt_publication
        or freeze_publication.get("publication_sha256")
        != workspace["passkey_request"].get(
            "freeze_publication_sha256"
        )
        or workspace["passkey_request"].get("plan_sha256") != freeze.sha256
    ):
        raise OwnerCutoverError("owner_cutover_workspace_invalid")
    request = _validate_cutover_passkey_request(
        workspace["passkey_request"],
        release_revision=release_revision,
        freeze_plan_sha256=freeze.sha256,
        freeze_publication_sha256=freeze_publication[
            "publication_sha256"
        ],
    )
    expected_bootstrap = _build_bridge_bootstrap_input(
        release_revision=release_revision,
        freeze_plan_sha256=freeze.sha256,
        freeze_approval_sha256=approval["approval_sha256"],
        freeze_publication_sha256=freeze_publication[
            "publication_sha256"
        ],
        passkey_request=request,
    )
    bridge_input = _validate_bridge_bootstrap_input(
        workspace["bridge_bootstrap_input"]
    )
    if bridge_input != expected_bootstrap:
        raise OwnerCutoverError("owner_cutover_workspace_invalid")

    gates = copy.deepcopy(list(workspace["gates"]))

    def require_v2_fresh() -> int:
        current = gate_now()
        if (
            current + MINIMUM_V2_APPROVAL_MARGIN_SECONDS
            >= bridge_input["v2_expires_at_unix"]
        ):
            raise OwnerCutoverError(
                "owner_cutover_v2_approval_window_stale"
            )
        return current

    def record(stage: str, value: Mapping[str, Any]) -> None:
        gates.append({
            "sequence": len(gates),
            "stage": stage,
            "evidence_sha256": _sha(_canonical(value)),
        })

    def next_workspace(**updates: Any) -> Mapping[str, Any]:
        unsigned = copy.deepcopy(dict(unsigned_workspace))
        unsigned.update(updates)
        unsigned["gates"] = copy.deepcopy(gates)
        return {
            **unsigned,
            "workspace_sha256": _sha(_canonical(unsigned)),
        }

    if state == "awaiting_bridge_bootstrap":
        if (
            workspace.get("bridge_request") is not None
            or workspace.get("bridge_receipt") is not None
            or workspace.get("advertised_approval_url") is not None
            or not callable(getattr(bridge_bootstrap, "prepare", None))
        ):
            raise OwnerCutoverError(
                "owner_cutover_bridge_bootstrap_invalid"
            )
        require_v2_fresh()
        bridge_request = _validate_bridge_request(
            bridge_bootstrap.prepare(bridge_input),
            document=bridge_input,
        )
        record("legacy_bridge_passkey_requested", bridge_request)
        return next_workspace(
            state="awaiting_bridge_passkey",
            bridge_request=bridge_request,
            production_host_mutation_performed=True,
        )

    if state == "awaiting_bridge_passkey":
        if (
            not isinstance(workspace.get("bridge_request"), Mapping)
            or workspace.get("bridge_receipt") is not None
            or workspace.get("advertised_approval_url") is not None
            or not callable(
                getattr(bridge_bootstrap, "consume_and_install", None)
            )
        ):
            raise OwnerCutoverError("owner_cutover_bridge_request_invalid")
        bridge_request = _validate_bridge_request(
            workspace["bridge_request"],
            document=bridge_input,
        )
        require_v2_fresh()
        bridge_receipt = _validate_bridge_receipt(
            bridge_bootstrap.consume_and_install(bridge_input),
            document=bridge_input,
            bridge_request=bridge_request,
        )
        record("approval_bridge_installed", bridge_receipt)
        return next_workspace(
            state="awaiting_cutover_passkey",
            bridge_receipt=bridge_receipt,
            advertised_approval_url=request["approval_url"],
        )

    if state == "awaiting_cutover_passkey" and (
        not isinstance(workspace.get("bridge_request"), Mapping)
        or not isinstance(workspace.get("bridge_receipt"), Mapping)
        or workspace.get("advertised_approval_url")
        != request["approval_url"]
        or not callable(getattr(passkey_boundary, "consume", None))
    ):
        raise OwnerCutoverError("owner_cutover_bridge_receipt_invalid")
    bridge_receipt = _validate_bridge_receipt(
        workspace["bridge_receipt"],
        document=bridge_input,
        bridge_request=workspace["bridge_request"],
    )
    consume_attempt_id = _sha(_canonical({
        "schema": "muncho-production-cutover-consume-attempt.v1",
        "release_revision": release_revision,
        "freeze_plan_sha256": freeze.sha256,
        "freeze_publication_sha256": freeze_publication[
            "publication_sha256"
        ],
        "request_id": request["request_id"],
        "operation": "claim_exact_freeze_plan",
    }))
    if state == "awaiting_cutover_passkey":
        consumed = passkey_boundary.consume(
            freeze_publication=freeze_publication,
            request_id=request["request_id"],
            consume_attempt_id=consume_attempt_id,
        )
    else:
        consumed = copy.deepcopy(dict(workspace["passkey_consumption"]))
    if (
        not isinstance(consumed, Mapping)
        or consumed.get("request_id") != request["request_id"]
        or consumed.get("consume_attempt_id") != consume_attempt_id
        or consumed.get("release_sha") != release_revision
        or consumed.get("plan_sha256") != freeze.sha256
        or consumed.get("single_use") is not True
        or consumed.get("control_plane_mutation_performed") is not True
        or consumed.get("source_data_mutation_performed") is not False
        or consumed.get("production_host_mutation_performed") is not False
        or consumed.get("disposition") not in {
            "authorized_once",
            "receipt_replay",
        }
    ):
        raise OwnerCutoverError(
            "owner_cutover_passkey_consumption_invalid"
        )
    proof = consumed.get("passkey_proof")
    if not isinstance(proof, Mapping):
        raise OwnerCutoverError("owner_cutover_passkey_proof_invalid")
    consumed_at = proof.get("authorization_receipt", {}).get(
        "consumed_at_unix"
    )
    if type(consumed_at) is not int or consumed_at <= 0:
        raise OwnerCutoverError("owner_cutover_passkey_proof_invalid")
    try:
        if state in {"passkey_claim_recorded", "cutover_staged"}:
            claim_frame = copy.deepcopy(dict(workspace["claim_frame"]))
            recorded_publication, recorded_proof = (
                cutover_passkey.validate_claim_frame_for_recorded_replay(
                    claim_frame
                )
            )
            if (
                recorded_publication != freeze_publication
                or recorded_proof != proof
            ):
                raise cutover_passkey.ProductionCutoverPasskeyError(
                    "production_cutover_passkey_claim_invalid"
                )
        else:
            claim_now = gate_now()
            if consumed.get("disposition") == "receipt_replay" and (
                type(consumed_at) is int and claim_now >= proof[
                    "authorization_receipt"
                ]["execution_window_expires_at_unix"]
            ):
                claim_now = consumed_at
            claim_frame = cutover_passkey.build_claim_frame(
                publication=freeze_publication,
                passkey_proof=proof,
                now_unix=claim_now,
            )
    except cutover_passkey.ProductionCutoverPasskeyError:
        raise OwnerCutoverError(
            "owner_cutover_passkey_proof_invalid"
        ) from None
    if state == "awaiting_cutover_passkey":
        record("single_use_passkey_consumed", {
            "consume_attempt_id": consume_attempt_id,
            "proof_sha256": proof["proof_sha256"],
            "authorization_receipt_sha256": proof[
                "authorization_receipt"
            ]["receipt_sha256"],
            "claim_sha256": claim_frame["claim_sha256"],
        })
        return next_workspace(
            state="passkey_claim_recorded",
            passkey_consumption=copy.deepcopy(dict(consumed)),
            claim_frame=claim_frame,
        )

    if state == "cutover_staged":
        try:
            tail = cutover.FinalTailReceipt.from_mapping(
                workspace["final_tail_receipt"],
                plan=freeze,
            )
            if tail.value["approval_sha256"] != approval["approval_sha256"]:
                raise OwnerCutoverError(
                    "owner_cutover_final_tail_authority_mismatch"
                )
            stopped_value = workspace["stopped_collector_receipt"]
            stopped = validate_stopped_collector_receipt(
                stopped_value,
                freeze_plan=freeze.to_mapping(),
                freeze_approval=approval,
                now_unix=stopped_value["observed_at_unix"],
                approval_now_unix=consumed_at,
            )
            cron_stage = _validate_cron_continuity_stage_receipt(
                workspace["cron_continuity_stage_receipt"],
                freeze_plan=freeze,
            )
            durable_plan = cutover.CutoverPlan.from_mapping(
                workspace["cutover_plan"]
            )
            durable_publication = copy.deepcopy(
                dict(workspace["cutover_publication"])
            )
            rebuilt_plan, rebuilt_publication = author_cutover(
                freeze_plan=freeze.to_mapping(),
                freeze_approval=approval,
                final_tail_receipt=tail.to_mapping(),
                gateway_stopped=stopped["gateway_stopped"],
                writer_stopped=stopped["writer_stopped"],
                connector_stopped=stopped["connector_stopped"],
                now_unix=tail.value["captured_at_unix"],
                approval_now_unix=consumed_at,
            )
            durable_stage = _validate_publication_stage_receipt(
                workspace["cutover_stage_receipt"],
                publication=durable_publication,
                expected_file_count=1,
            )
        except (
            KeyError,
            OwnerCutoverError,
            PermissionError,
            PublicStagingError,
            TypeError,
            ValueError,
        ):
            raise OwnerCutoverError(
                "owner_cutover_workspace_invalid"
            ) from None
        if (
            durable_plan.to_mapping() != rebuilt_plan.to_mapping()
            or durable_publication != rebuilt_publication
            or durable_stage != workspace["cutover_stage_receipt"]
            or cron_stage != workspace["cron_continuity_stage_receipt"]
        ):
            raise OwnerCutoverError("owner_cutover_workspace_invalid")
        transport = transport_factory(owner_identity)
        convergence = _validate_convergence_receipt(
            transport.invoke(release_revision, "converge-cutover"),
            plan=durable_plan,
        )
        record("cutover_convergence_accepted", convergence)
        workflow_receipt = _build_workflow_receipt(
            release_revision=release_revision,
            freeze_plan_sha256=freeze.sha256,
            freeze_approval_sha256=approval["approval_sha256"],
            cutover_plan_sha256=durable_plan.sha256,
            convergence=convergence,
            gates=gates,
        )
        try:
            host_receipt = copy.deepcopy(dict(transport.invoke(
                release_revision, "collect-bootstrap-target"
            )))
            host_unsigned = {
                name: item
                for name, item in host_receipt.items()
                if name != "receipt_sha256"
            }
            if (
                host_receipt.get("schema")
                != initial_bootstrap.HOST_RECEIPT_SCHEMA
                or host_receipt.get("legacy_predecessor_revision")
                != workspace["legacy_predecessor_revision"]
                or host_receipt.get("release_revision") != release_revision
                or host_receipt.get("receipt_sha256")
                != _sha(_canonical(host_unsigned))
            ):
                raise OwnerCutoverError(
                    "owner_cutover_bootstrap_host_receipt_invalid"
                )
            terminal = _validate_terminal_receipt(
                host_receipt["cutover_terminal_receipt"],
                plan=durable_plan,
            )
            if (
                terminal["receipt_sha256"]
                != convergence["cutover_terminal_receipt_sha256"]
            ):
                raise OwnerCutoverError(
                    "owner_cutover_bootstrap_host_receipt_invalid"
                )
            activation_receipt = (
                initial_bootstrap.build_terminal_activation_receipt(
                    legacy_predecessor_revision=workspace[
                        "legacy_predecessor_revision"
                    ],
                    release_revision=release_revision,
                    freeze_plan=freeze.to_mapping(),
                    freeze_approval=approval,
                    cutover_plan=durable_plan.to_mapping(),
                    cutover_terminal_receipt=terminal,
                    convergence_receipt=convergence,
                    workflow_receipt=workflow_receipt,
                    host_receipt=host_receipt,
                )
            )
            return initial_bootstrap.build_terminal_envelope(
                workflow_receipt=workflow_receipt,
                activation_receipt=activation_receipt,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            initial_bootstrap.ProductionInitialReleaseBootstrapError,
        ) as exc:
            raise OwnerCutoverError(
                "owner_cutover_bootstrap_terminal_invalid"
            ) from exc

    transport = transport_factory(owner_identity)
    freeze_staged = False
    cutover_staged = False
    try:
        stage_receipt = _validate_publication_stage_receipt(
            transport.invoke(
                release_revision,
                "stage-publication",
                publication=claim_frame,
            ),
            publication=freeze_publication,
            expected_file_count=2,
        )
        freeze_staged = True
        record("freeze_authority_staged", stage_receipt)
        tail = cutover.FinalTailReceipt.from_mapping(
            transport.invoke(release_revision, "capture-final-tail"),
            plan=freeze,
        )
        if tail.value["approval_sha256"] != approval["approval_sha256"]:
            raise OwnerCutoverError(
                "owner_cutover_final_tail_authority_mismatch"
            )
        record("final_tail_captured", tail.to_mapping())
        stopped = validate_stopped_collector_receipt(
            transport.invoke(release_revision, "collect-stopped"),
            freeze_plan=freeze.to_mapping(),
            freeze_approval=approval,
            now_unix=gate_now(),
            approval_now_unix=consumed_at,
        )
        record("stopped_services_collected", stopped)
        cron_stage = _validate_cron_continuity_stage_receipt(
            transport.invoke(release_revision, "stage-cron-continuity"),
            freeze_plan=freeze,
        )
        record("cron_continuity_stage_accepted", cron_stage)
        cutover_plan, cutover_publication = author_cutover(
            freeze_plan=freeze.to_mapping(),
            freeze_approval=approval,
            final_tail_receipt=tail.to_mapping(),
            gateway_stopped=stopped["gateway_stopped"],
            writer_stopped=stopped["writer_stopped"],
            connector_stopped=stopped["connector_stopped"],
            now_unix=tail.value["captured_at_unix"],
            approval_now_unix=consumed_at,
        )
        record("cutover_plan_composed", {
            "plan_sha256": cutover_plan.sha256,
            "publication_sha256": cutover_publication[
                "publication_sha256"
            ],
        })
        cutover_stage_receipt = _validate_publication_stage_receipt(
            transport.invoke(
                release_revision,
                "stage-publication",
                publication=cutover_publication,
            ),
            publication=cutover_publication,
            expected_file_count=1,
        )
        cutover_staged = True
        record("cutover_plan_staged", cutover_stage_receipt)
        return next_workspace(
            state="cutover_staged",
            final_tail_receipt=tail.to_mapping(),
            stopped_collector_receipt=copy.deepcopy(dict(stopped)),
            cron_continuity_stage_receipt=copy.deepcopy(dict(cron_stage)),
            cutover_plan=cutover_plan.to_mapping(),
            cutover_publication=copy.deepcopy(dict(cutover_publication)),
            cutover_stage_receipt=copy.deepcopy(dict(cutover_stage_receipt)),
        )
    except BaseException as primary:
        if freeze_staged and not cutover_staged:
            try:
                aborted = transport.invoke(release_revision, "abort-freeze")
                if cutover._validate_freeze_abort_receipt(
                    aborted, plan=freeze
                )["approval_sha256"] != approval["approval_sha256"]:
                    raise OwnerCutoverError(
                        "owner_cutover_freeze_abort_receipt_invalid"
                    )
            except BaseException as abort_error:
                raise BaseExceptionGroup(
                    "production cutover failed and freeze abort was incomplete",
                    [primary, abort_error],
                ) from None
        raise


def _validate_public_output_path(path: Path) -> None:
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise OwnerCutoverError("owner_cutover_output_path_invalid")
    parent = path.parent.lstat()
    if (
        stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()  # windows-footgun: ok — macOS/Linux owner boundary
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise OwnerCutoverError("owner_cutover_output_parent_invalid")


def _write_public_output(path: Path, value: Mapping[str, Any]) -> bool:
    _validate_public_output_path(path)
    payload = _canonical(value)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor: int | None = None
    created = False
    try:
        if os.path.lexists(path):
            if _canonical(_read_public_json(path)) != payload:
                raise OwnerCutoverError("owner_cutover_output_conflict")
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short owner cutover output write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, path, follow_symlinks=False)
            created = True
        except FileExistsError:
            pass
        temporary.unlink()
        if _canonical(_read_public_json(path)) != payload:
            raise OwnerCutoverError("owner_cutover_output_conflict")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return created


def build_production_os_login_metadata_boundary(
    release_revision: str,
) -> tuple[Any, Any]:
    """Construct the fixed production OS Login gate from sealed owner runtime."""

    identity, trusted, configuration = build_production_cutover_owner_identity(
        release_revision
    )
    transport = ProductionCutoverTransport(
        identity,
        gcloud_executable=trusted,
        gcloud_configuration=configuration,
    )
    from scripts.canary import production_os_login_metadata_migration as os_login

    return identity, os_login.ProductionOsLoginMetadataTransport(transport)


def build_production_cutover_owner_identity(
    release_revision: str,
) -> tuple[Any, Any, Any]:
    """Construct the one release-bound human Cloud identity and SDK."""

    if package.REVISION.fullmatch(release_revision or "") is None:
        raise OwnerCutoverError("owner_cutover_os_login_revision_invalid")
    _active_owner_runtime_attestation(release_revision)
    trusted = canary_transport.TrustedGcloudExecutable(
        release_sha=release_revision
    )
    configuration = canary_transport.PinnedGcloudConfiguration()
    identity = canary_transport.GcloudOwnerAccessToken(
        gcloud_executable=trusted,
        gcloud_configuration=configuration,
    )
    identity.account_for_read_only_preflight()
    return identity, trusted, configuration


def build_production_cutover_passkey_boundary(
    release_revision: str,
    *,
    owner_identity: Any,
    gcloud_executable: Any,
    gcloud_configuration: Any,
) -> cutover_passkey.ProductionCutoverPasskeyBoundary:
    """Construct only the fixed IAP owner-gate passkey intake boundary."""

    transport = canary_transport.OwnerGateIapTransport(
        release_sha=release_revision,
        owner_identity=owner_identity,
        gcloud_executable=gcloud_executable,
        gcloud_configuration=gcloud_configuration,
    )
    return cutover_passkey.ProductionCutoverPasskeyBoundary(
        release_revision, transport
    )


def _execute_os_login_migration_signal_safe(
    *,
    boundary: Any,
    authority: Mapping[str, Any],
    output_path: Path,
) -> tuple[Mapping[str, Any], bool]:
    """Keep rollback and terminal receipt persistence non-interruptible."""

    from scripts.canary import production_os_login_metadata_migration as os_login

    fence = canary_transport._OwnerSignalFence()
    fence.install()
    primary: BaseException | None = None
    receipt: Mapping[str, Any] | None = None
    created = False
    try:
        intent_path = output_path.with_name(
            f".{output_path.name}.migration-intent.json"
        )
        if os.path.lexists(intent_path):
            intent = os_login.validate_migration_intent(
                _read_public_json(intent_path),
                plan=authority["plan"],
                approval=authority["approval"],
            )
        else:
            intent = os_login.build_migration_intent(
                observed=boundary.observe(),
                plan=authority["plan"],
                approval=authority["approval"],
            )
            _write_public_output(intent_path, intent)
        receipt = os_login.execute_migration(
            boundary=boundary,
            plan=authority["plan"],
            approval=authority["approval"],
            intent=intent,
        )
    except BaseException as exc:
        primary = exc
    finally:
        # Once cleanup begins, further SIGINT/SIGTERM/SIGHUP are recorded and
        # suppressed.  If the Cloud mutation reached terminal truth, persist
        # its exact receipt even when the first signal arrived just after the
        # remote call returned.
        fence.begin_cleanup()
        if receipt is not None:
            try:
                created = _write_public_output(output_path, receipt)
            except BaseException as cleanup_error:
                primary = (
                    cleanup_error
                    if primary is None
                    else BaseExceptionGroup(
                        "OS Login migration failed during receipt persistence",
                        [primary, cleanup_error],
                    )
                )
        try:
            fence.restore()
        except BaseException as cleanup_error:
            primary = (
                cleanup_error
                if primary is None
                else BaseExceptionGroup(
                    "OS Login migration signal cleanup was incomplete",
                    [primary, cleanup_error],
                )
            )
    if primary is not None:
        raise primary
    if receipt is None:
        raise OwnerCutoverError("owner_cutover_os_login_terminal_missing")
    return receipt, created


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Author exact public production cutover publications",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    unit = subparsers.add_parser("author-unit-inputs")
    unit.add_argument("--revision", required=True)
    unit.add_argument("--unit-inputs", type=Path, required=True)
    unit.add_argument("--owner-private-key", type=Path, required=True)
    unit.add_argument("--owner-subject-sha256", required=True)
    unit.add_argument("--output", type=Path, required=True)
    derive_unit = subparsers.add_parser("derive-unit-inputs")
    derive_unit.add_argument("--revision", required=True)
    derive_unit.add_argument("--predecessor-plan", type=Path, required=True)
    derive_unit.add_argument(
        "--predecessor-approval",
        type=Path,
        required=True,
    )
    derive_unit.add_argument(
        "--predecessor-fixed-inputs",
        type=Path,
        required=True,
    )
    derive_unit.add_argument("--output", type=Path, required=True)
    stage_unit = subparsers.add_parser("stage-unit-inputs")
    stage_unit.add_argument("--revision", required=True)
    stage_unit.add_argument("--remote-stager-revision", required=True)
    stage_unit.add_argument("--publication", type=Path, required=True)
    stage_unit.add_argument("--output", type=Path, required=True)
    rotate_unit = subparsers.add_parser("rotate-unit-inputs")
    rotate_unit.add_argument("--revision", required=True)
    rotate_unit.add_argument("--remote-stager-revision", required=True)
    rotate_unit.add_argument("--publication", type=Path, required=True)
    rotate_unit.add_argument("--output", type=Path, required=True)
    for phase_command, phase_action in sorted(
        LOCAL_RELEASE_PHASE_COMMANDS.items()
    ):
        phase_parser = subparsers.add_parser(phase_command)
        phase_parser.add_argument("--revision", required=True)
        phase_parser.add_argument(
            "--remote-stager-revision",
            required=True,
        )
        phase_parser.add_argument(
            "--unit-input-publication",
            type=Path,
            required=True,
        )
        phase_parser.add_argument(
            "--release-update-publication",
            type=Path,
            required=True,
        )
        phase_parser.add_argument(
            "--trusted-predecessor",
            type=Path,
            required=True,
        )
        phase_parser.add_argument(
            "--expected-predecessor-trust-sha256",
            required=True,
        )
        if phase_action != "prepare-release-unit-inputs":
            phase_parser.add_argument(
                "--prepared-receipt",
                type=Path,
                required=True,
            )
            phase_parser.add_argument(
                "--expected-transaction-sha256",
                required=True,
            )
        if phase_action in {
            "finalize-release-unit-inputs",
            "abort-release-unit-inputs",
        }:
            phase_parser.add_argument(
                "--preauthorization-receipt",
                type=Path,
                required=True,
            )
        phase_parser.add_argument("--output", type=Path, required=True)
    os_login_preflight = subparsers.add_parser("os-login-preflight")
    os_login_preflight.add_argument("--revision", required=True)
    os_login_preflight.add_argument(
        "--owner-private-key", type=Path, required=True
    )
    os_login_preflight.add_argument("--output", type=Path, required=True)
    os_login_migrate = subparsers.add_parser("os-login-migrate")
    os_login_migrate.add_argument("--revision", required=True)
    os_login_migrate.add_argument("--authority", type=Path, required=True)
    os_login_migrate.add_argument("--output", type=Path, required=True)
    prepare = subparsers.add_parser("prepare-cutover")
    prepare.add_argument("--revision", required=True)
    prepare.add_argument(
        "--legacy-predecessor-revision",
        choices=(LEGACY_F5_PREDECESSOR_REVISION,),
        required=True,
    )
    validation_authority = prepare.add_mutually_exclusive_group(required=True)
    validation_authority.add_argument(
        "--isolated-canary-goal-prerequisite", type=Path
    )
    validation_authority.add_argument(
        "--owner-approved-direct-mvp-no-canary",
        action="store_true",
        help=(
            "owner-sign a narrow waiver of only the release-bound isolated "
            "canary proof; all live production and pre-DB gates remain"
        ),
    )
    prepare.add_argument("--owner-private-key", type=Path, required=True)
    prepare.add_argument(
        "--truth-mode",
        choices=("start_new_truth_epoch",),
        required=True,
    )
    prepare.add_argument("--output", type=Path, required=True)
    resume = subparsers.add_parser("resume-cutover")
    resume.add_argument("--revision", required=True)
    resume.add_argument("--workspace", type=Path, required=True)
    resume.add_argument("--output", type=Path, required=True)
    successor_rebind = subparsers.add_parser(
        "upstream-sync-successor-owner-apply"
    )
    successor_rebind.add_argument("--revision", required=True)
    successor_rebind.add_argument(
        "--launch-authority-sha256",
        required=True,
    )
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "upstream-sync-successor-owner-apply":
            if (
                _SHA256.fullmatch(arguments.launch_authority_sha256 or "")
                is None
            ):
                parser.error("--launch-authority-sha256 must be 64 lowercase hex")
            _active_owner_runtime_attestation(arguments.revision)
            from scripts.canary import (
                upstream_sync_rail_successor_rebind as successor_rebind_runtime,
            )

            result = successor_rebind_runtime.owner_apply_framed_stdin(
                expected_launch_authority_sha256=(
                    arguments.launch_authority_sha256
                )
            )
            print(_canonical(result).decode("ascii"))
            return 0
        if not arguments.output.is_absolute():
            raise OwnerCutoverError("owner_cutover_output_path_invalid")
        runtime_attestation = _active_owner_runtime_attestation(
            arguments.revision
        )
        if arguments.command == "derive-unit-inputs":
            predecessor_paths = (
                arguments.predecessor_plan,
                arguments.predecessor_approval,
                arguments.predecessor_fixed_inputs,
            )
            if any(not path.is_absolute() for path in predecessor_paths):
                raise OwnerCutoverError(
                    "owner_cutover_public_input_invalid"
                )
            predecessor_plan = _read_public_json(
                arguments.predecessor_plan
            )
            output_value = unit_successor.derive_successor_payload(
                predecessor_plan=predecessor_plan,
                predecessor_approval=_read_public_json(
                    arguments.predecessor_approval
                ),
                predecessor_fixed_inputs=_read_public_json_line(
                    arguments.predecessor_fixed_inputs
                ),
                successor_revision=arguments.revision,
            )
            created = _write_public_output(arguments.output, output_value)
            print(_canonical({
                "schema": OWNER_WORKSPACE_SCHEMA,
                "action": arguments.command,
                "predecessor_revision": predecessor_plan[
                    "release_revision"
                ],
                "release_revision": arguments.revision,
                "output_path": str(arguments.output),
                "output_sha256": _sha(_canonical(output_value)),
                "created": created,
                "private_key_staged": False,
                "secret_material_recorded": False,
            }).decode("utf-8"))
            return 0
        if arguments.command in LOCAL_RELEASE_PHASE_COMMANDS:
            phase_action = LOCAL_RELEASE_PHASE_COMMANDS[arguments.command]
            input_paths = (
                arguments.unit_input_publication,
                arguments.release_update_publication,
                arguments.trusted_predecessor,
                getattr(arguments, "prepared_receipt", None),
                getattr(arguments, "preauthorization_receipt", None),
            )
            if any(
                path is not None and not path.is_absolute()
                for path in input_paths
            ):
                raise OwnerCutoverError(
                    "owner_cutover_release_unit_input_path_invalid"
                )
            phase_kwargs = {
                "action": phase_action,
                "owner_release_revision": arguments.revision,
                "remote_stager_revision": (
                    arguments.remote_stager_revision
                ),
                "unit_input_publication": _read_public_json(
                    arguments.unit_input_publication
                ),
                "release_update_publication": _read_public_json(
                    arguments.release_update_publication
                ),
                "trusted_predecessor": _read_public_json(
                    arguments.trusted_predecessor
                ),
                "expected_predecessor_trust_sha256": (
                    arguments.expected_predecessor_trust_sha256
                ),
                "prepared_receipt": (
                    None
                    if getattr(arguments, "prepared_receipt", None) is None
                    else _read_public_json(arguments.prepared_receipt)
                ),
                "preauthorization_receipt": (
                    None
                    if getattr(
                        arguments,
                        "preauthorization_receipt",
                        None,
                    )
                    is None
                    else _read_public_json(
                        arguments.preauthorization_receipt
                    )
                ),
                "expected_transaction_sha256": getattr(
                    arguments,
                    "expected_transaction_sha256",
                    None,
                ),
            }
            if phase_action in {
                "prepare-release-unit-inputs",
                "preauthorize-release-unit-inputs",
            }:
                identity, trusted, configuration = (
                    build_production_cutover_owner_identity(
                        arguments.revision
                    )
                )
                output_value = run_release_unit_input_phase(
                    **phase_kwargs,
                    transport=ProductionCutoverTransport(
                        identity,
                        gcloud_executable=trusted,
                        gcloud_configuration=configuration,
                    ),
                )
                output_sha256 = output_value["result_sha256"]
                receipt_summary = {
                    "canonical_receipt_sha256": output_value[
                        "canonical_receipt_sha256"
                    ],
                }
            else:
                output_value = build_release_unit_input_phase_request(
                    **phase_kwargs,
                )
                output_sha256 = output_value["request_sha256"]
                receipt_summary = {
                    "sealed_request_action": phase_action,
                    "sealed_request_sha256": output_sha256,
                }
            created = _write_public_output(arguments.output, output_value)
            print(_canonical({
                "schema": OWNER_WORKSPACE_SCHEMA,
                "action": arguments.command,
                "release_revision": arguments.revision,
                "remote_stager_revision": (
                    arguments.remote_stager_revision
                ),
                "output_path": str(arguments.output),
                "output_sha256": output_sha256,
                **receipt_summary,
                "created": created,
                "private_key_staged": False,
                "secret_material_recorded": False,
            }).decode("utf-8"))
            return 0
        if arguments.command in {"stage-unit-inputs", "rotate-unit-inputs"}:
            if not arguments.publication.is_absolute():
                raise OwnerCutoverError(
                    "owner_cutover_public_input_invalid"
                )
            identity, trusted, configuration = (
                build_production_cutover_owner_identity(
                    arguments.revision
                )
            )
            operation = (
                stage_unit_input_authority
                if arguments.command == "stage-unit-inputs"
                else rotate_unit_input_authority
            )
            output_value = operation(
                release_revision=arguments.revision,
                remote_stager_revision=arguments.remote_stager_revision,
                publication=_read_public_json(arguments.publication),
                transport=ProductionCutoverTransport(
                    identity,
                    gcloud_executable=trusted,
                    gcloud_configuration=configuration,
                ),
            )
            created = _write_public_output(arguments.output, output_value)
            print(_canonical({
                "schema": OWNER_WORKSPACE_SCHEMA,
                "action": arguments.command,
                "release_revision": arguments.revision,
                "remote_stager_revision": (
                    arguments.remote_stager_revision
                ),
                "output_path": str(arguments.output),
                "output_sha256": output_value["receipt_sha256"],
                "created": created,
                "private_key_staged": False,
                "secret_material_recorded": False,
            }).decode("utf-8"))
            return 0
        if arguments.command == "prepare-cutover":
            if (
                not arguments.owner_private_key.is_absolute()
                or (
                    arguments.isolated_canary_goal_prerequisite is not None
                    and not arguments.isolated_canary_goal_prerequisite.is_absolute()
                )
            ):
                raise OwnerCutoverError(
                    "owner_cutover_workflow_input_invalid"
                )
            identity, trusted, configuration = (
                build_production_cutover_owner_identity(
                    arguments.revision
                )
            )
            owner_subject = identity.owner_subject_sha256
            if (
                not isinstance(owner_subject, str)
                or _SHA256.fullmatch(owner_subject) is None
            ):
                raise OwnerCutoverError(
                    "owner_cutover_workflow_identity_invalid"
                )
            canary_transport.harden_owner_secret_process()
            key = load_owner_private_key(arguments.owner_private_key)
            release_validation_authority = (
                cutover.build_owner_direct_mvp_no_canary_waiver(
                    release_revision=arguments.revision,
                    owner_subject_sha256=owner_subject,
                    owner_public_key_ed25519_hex=_public_hex(key),
                )
                if arguments.owner_approved_direct_mvp_no_canary
                else _read_public_json(
                    arguments.isolated_canary_goal_prerequisite
                )
            )

            def transport_factory(owner_identity: Any) -> Any:
                return ProductionCutoverTransport(
                    owner_identity,
                    gcloud_executable=trusted,
                    gcloud_configuration=configuration,
                )

            passkey_boundary = build_production_cutover_passkey_boundary(
                arguments.revision,
                owner_identity=identity,
                gcloud_executable=trusted,
                gcloud_configuration=configuration,
            )
            output_value = execute_production_cutover_workflow(
                release_revision=arguments.revision,
                owner_identity=identity,
                owner_subject_sha256=owner_subject,
                private_key=key,
                isolated_canary_goal_prerequisite=(
                    release_validation_authority
                ),
                legacy_predecessor_revision=(
                    arguments.legacy_predecessor_revision
                ),
                truth_mode=arguments.truth_mode,
                accepted_event_receipts=None,
                passkey_proof=None,
                passkey_boundary=passkey_boundary,
                prepare_only=True,
                transport_factory=transport_factory,
            )
            created = _write_public_output(arguments.output, output_value)
            print(_canonical({
                "schema": OWNER_WORKSPACE_SCHEMA,
                "action": arguments.command,
                "output_path": str(arguments.output),
                "output_sha256": output_value.get(
                    "receipt_sha256", output_value.get("workspace_sha256")
                ),
                "created": created,
                "private_key_staged": False,
                "secret_material_recorded": False,
            }).decode("utf-8"))
            return 0
        if arguments.command == "resume-cutover":
            if not arguments.workspace.is_absolute():
                raise OwnerCutoverError("owner_cutover_workspace_invalid")
            identity, trusted, configuration = (
                build_production_cutover_owner_identity(arguments.revision)
            )
            passkey_boundary = build_production_cutover_passkey_boundary(
                arguments.revision,
                owner_identity=identity,
                gcloud_executable=trusted,
                gcloud_configuration=configuration,
            )
            bridge_bootstrap = ProductionCutoverBridgeBootstrap(
                arguments.revision,
                ProductionCutoverTransport(
                    identity,
                    gcloud_executable=trusted,
                    gcloud_configuration=configuration,
                ),
            )

            def transport_factory(owner_identity: Any) -> Any:
                return ProductionCutoverTransport(
                    owner_identity,
                    gcloud_executable=trusted,
                    gcloud_configuration=configuration,
                )

            output_value = resume_prepared_production_cutover_workflow(
                workspace=_read_public_json(arguments.workspace),
                owner_identity=identity,
                passkey_boundary=passkey_boundary,
                bridge_bootstrap=bridge_bootstrap,
                transport_factory=transport_factory,
            )
            created = _write_public_output(arguments.output, output_value)
            print(_canonical({
                "schema": OWNER_WORKSPACE_SCHEMA,
                "action": arguments.command,
                "output_path": str(arguments.output),
                "output_sha256": output_value.get(
                    "receipt_sha256", output_value.get("workspace_sha256")
                ),
                "created": created,
                "private_key_staged": False,
                "secret_material_recorded": False,
            }).decode("utf-8"))
            return 0
        if arguments.command in {"os-login-preflight", "os-login-migrate"}:
            from scripts.canary import (
                production_os_login_metadata_migration as os_login,
            )

            _validate_public_output_path(arguments.output)
            if (
                arguments.command == "os-login-migrate"
                and os.path.lexists(arguments.output)
            ):
                raise OwnerCutoverError("owner_cutover_output_conflict")
            identity, boundary = build_production_os_login_metadata_boundary(
                arguments.revision
            )
            if arguments.command == "os-login-preflight":
                preflight = os_login.collect_migration_preflight(boundary)
                owner_subject = identity.owner_subject_sha256
                if (
                    not isinstance(owner_subject, str)
                    or _SHA256.fullmatch(owner_subject) is None
                    or not arguments.owner_private_key.is_absolute()
                ):
                    raise OwnerCutoverError(
                        "owner_cutover_os_login_authority_invalid"
                    )
                canary_transport.harden_owner_secret_process()
                key = load_owner_private_key(arguments.owner_private_key)
                plan, approval = os_login.build_migration_plan(
                    preflight_receipt=preflight,
                    owner_subject_sha256=owner_subject,
                    private_key=key,
                )
                output_value = os_login.build_authority_bundle(
                    release_revision=arguments.revision,
                    preflight=preflight,
                    plan=plan,
                    approval=approval,
                )
                output_sha256 = output_value["authority_sha256"]
            else:
                if not arguments.authority.is_absolute():
                    raise OwnerCutoverError(
                        "owner_cutover_os_login_authority_invalid"
                    )
                intent_path = arguments.output.with_name(
                    f".{arguments.output.name}.migration-intent.json"
                )
                recovery_now = None
                if os.path.lexists(intent_path):
                    recovery_intent = _read_public_json(intent_path)
                    candidate = recovery_intent.get("created_at_unix")
                    if type(candidate) is not int:
                        raise OwnerCutoverError(
                            "owner_cutover_os_login_authority_invalid"
                        )
                    recovery_now = candidate
                authority = os_login.validate_authority_bundle(
                    _read_public_json(arguments.authority),
                    release_revision=arguments.revision,
                    now_unix=recovery_now,
                )
                output_value, created = _execute_os_login_migration_signal_safe(
                    boundary=boundary,
                    authority=authority,
                    output_path=arguments.output,
                )
                output_sha256 = output_value["receipt_sha256"]
            if arguments.command == "os-login-preflight":
                created = _write_public_output(arguments.output, output_value)
            print(
                _canonical({
                    "schema": OWNER_WORKSPACE_SCHEMA,
                    "action": arguments.command,
                    "output_path": str(arguments.output),
                    "output_sha256": output_sha256,
                    "created": created,
                    "private_key_staged": False,
                    "secret_material_recorded": False,
                }).decode("utf-8")
            )
            return 0
        if not arguments.owner_private_key.is_absolute():
            raise OwnerCutoverError("owner_cutover_private_key_invalid")
        key = load_owner_private_key(arguments.owner_private_key)
        if not arguments.unit_inputs.is_absolute():
            raise OwnerCutoverError("owner_cutover_public_input_invalid")
        _plan, _approval, publication = build_unit_input_authority(
            release_revision=arguments.revision,
            unit_inputs=_read_public_json(arguments.unit_inputs),
            owner_subject_sha256=arguments.owner_subject_sha256,
            private_key=key,
            owner_runtime_attestation=runtime_attestation,
        )
        created = _write_public_output(arguments.output, publication)
    except (
        OSError,
        OwnerCutoverError,
        canary_transport.OwnerLauncherError,
        PermissionError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        print('{"error_code":"owner_cutover_authoring_failed","ok":false}', file=sys.stderr)
        return 2
    print(_canonical({
        "schema": OWNER_WORKSPACE_SCHEMA,
        "action": publication["action"],
        "publication_path": str(arguments.output),
        "publication_sha256": publication["publication_sha256"],
        "created": created,
        "private_key_staged": False,
        "secret_material_recorded": False,
    }).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
