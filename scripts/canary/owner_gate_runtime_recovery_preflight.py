#!/usr/bin/env python3
"""Read-only owner-gate runtime restore and health preflight.

The preflight accepts one exact release revision.  It binds the installed
immutable release tree to the Ed25519-signed inert install receipt emitted by
``owner_gate_bootstrap``, verifies the installed systemd unit bytes against
that release, and then classifies only two complete host states:

* ``restore_required_inert``: the current link is absent and every installed
  owner-gate unit is disabled and inactive.
* ``ready``: the current link selects the exact release, the five units in the
  package's fixed activation order have their exact healthy states, and the
  remaining three helper services are disabled and inactive.

Any missing release, partial activation, divergent link, changed release
byte, changed unit byte, unsigned evidence, or unfamiliar systemd observation
fails closed.  This module has no file-writing or systemd-mutating command.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.canary import owner_gate_bootstrap as bootstrap
from scripts.canary import owner_gate_foundation as foundation


PREFLIGHT_SCHEMA = "muncho-owner-gate-runtime-recovery-preflight.v1"
INSTALLED_UNITS = tuple(bootstrap.SYSTEMD_ASSETS)
READY_UNIT_STATES: Mapping[str, tuple[str, str, str]] = {
    "muncho-owner-gate-metadata-firewall.service": (
        "active",
        "exited",
        "enabled",
    ),
    "muncho-owner-gate-firewall-readiness.service": (
        "inactive",
        "dead",
        "disabled",
    ),
    "muncho-owner-gate-firewall-readiness.timer": (
        "active",
        "waiting",
        "enabled",
    ),
    "muncho-passkey-authority.service": (
        "inactive",
        "dead",
        "disabled",
    ),
    "muncho-passkey-authority.socket": (
        "active",
        "listening",
        "enabled",
    ),
    "muncho-passkey-web.service": ("active", "running", "enabled"),
    "muncho-privileged-executor.service": (
        "inactive",
        "dead",
        "disabled",
    ),
    "muncho-privileged-executor.socket": (
        "active",
        "listening",
        "enabled",
    ),
}
SYSTEMD_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "FragmentPath",
    "DropInPaths",
    "NeedDaemonReload",
)
REQUIRED_RUNTIME_ENTRYPOINTS = (
    "bin/muncho-passkey-v2-authority",
    "bin/muncho-passkey-v2-web",
    "bin/muncho-passkey-v2-executor",
)
MAX_SYSTEMD_OUTPUT_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OwnerGateRuntimePreflightError(RuntimeError):
    """Stable, secret-free runtime preflight failure."""


class SystemdStateObserver(Protocol):
    def observe(
        self,
        units: Sequence[str],
    ) -> Mapping[str, Mapping[str, str]]: ...


def _error(code: str) -> None:
    raise OwnerGateRuntimePreflightError(code) from None


class SystemdObserver:
    """Observe only a caller-provided tuple of fixed systemd unit names."""

    def observe(
        self,
        units: Sequence[str],
    ) -> Mapping[str, Mapping[str, str]]:
        requested = tuple(units)
        if (
            not requested
            or len(set(requested)) != len(requested)
            or any(unit not in INSTALLED_UNITS for unit in requested)
        ):
            _error("owner_gate_runtime_systemd_query_invalid")
        argv = (
            "/usr/bin/systemctl",
            "show",
            "--no-pager",
            f"--property={','.join(SYSTEMD_PROPERTIES)}",
            *requested,
        )
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin",
                },
                shell=False,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            _error("owner_gate_runtime_systemd_observation_failed")
        if (
            completed.returncode != 0
            or completed.stderr
            or not completed.stdout
            or len(completed.stdout) > MAX_SYSTEMD_OUTPUT_BYTES
        ):
            _error("owner_gate_runtime_systemd_observation_failed")
        try:
            text = completed.stdout.decode("ascii", errors="strict")
        except UnicodeError:
            _error("owner_gate_runtime_systemd_observation_invalid")
        blocks = text.strip().split("\n\n")
        observed: dict[str, dict[str, str]] = {}
        try:
            for block in blocks:
                values: dict[str, str] = {}
                for line in block.splitlines():
                    key, separator, value = line.partition("=")
                    if separator != "=" or not key or key in values:
                        raise ValueError
                    values[key] = value
                unit = values.get("Id")
                if (
                    frozenset(values) != frozenset(SYSTEMD_PROPERTIES)
                    or not isinstance(unit, str)
                    or unit not in requested
                    or unit in observed
                ):
                    raise ValueError
                observed[unit] = values
        except ValueError:
            _error("owner_gate_runtime_systemd_observation_invalid")
        if set(observed) != set(requested):
            _error("owner_gate_runtime_systemd_observation_invalid")
        return observed


def _read_owned_regular(
    path: Path,
    *,
    maximum: int,
    expected_uid: int,
    expected_gid: int,
    modes: frozenset[int],
    error_code: str,
) -> bytes:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid
            or before.st_gid != expected_gid
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) not in modes
            or not 0 < before.st_size <= maximum
        ):
            _error(error_code)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_nlink,
            opened.st_uid,
            opened.st_gid,
            opened.st_size,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_uid,
            before.st_gid,
            before.st_size,
        ):
            _error(error_code)
        raw = bytearray()
        while len(raw) < opened.st_size:
            chunk = os.read(descriptor, min(64 * 1024, opened.st_size - len(raw)))
            if not chunk:
                _error(error_code)
            raw.extend(chunk)
        if os.read(descriptor, 1):
            _error(error_code)
        after = os.fstat(descriptor)
        reachable = path.lstat()
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_uid,
            after.st_gid,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_nlink,
            opened.st_uid,
            opened.st_gid,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) or (
            reachable.st_dev,
            reachable.st_ino,
            reachable.st_mode,
            reachable.st_nlink,
            reachable.st_uid,
            reachable.st_gid,
            reachable.st_size,
            reachable.st_mtime_ns,
            reachable.st_ctime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_nlink,
            opened.st_uid,
            opened.st_gid,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            _error(error_code)
        return bytes(raw)
    except OwnerGateRuntimePreflightError:
        raise
    except OSError:
        _error(error_code)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_install_receipt(
    revision: str,
    *,
    layout: bootstrap.InstallLayout,
    expected_uid: int,
    expected_gid: int,
) -> tuple[Mapping[str, Any], str]:
    receipt_path = layout.state_root / "bootstrap-receipts" / f"install-{revision}.json"
    raw = _read_owned_regular(
        receipt_path,
        maximum=MAX_EVIDENCE_BYTES,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        modes=frozenset({0o400}),
        error_code="owner_gate_runtime_install_receipt_invalid",
    )
    try:
        receipt = bootstrap._canonical_json(raw)
    except bootstrap.OwnerGateBootstrapError:
        _error("owner_gate_runtime_install_receipt_invalid")

    public_path = layout.etc_root / "public/authority-receipt-public.pem"
    public_raw = _read_owned_regular(
        public_path,
        maximum=4096,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        modes=frozenset({0o444}),
        error_code="owner_gate_runtime_install_receipt_invalid",
    )
    try:
        public_key = serialization.load_pem_public_key(public_raw)
    except (TypeError, ValueError):
        _error("owner_gate_runtime_install_receipt_invalid")
    if not isinstance(public_key, Ed25519PublicKey):
        _error("owner_gate_runtime_install_receipt_invalid")
    public_key_id = hashlib.sha256(public_key.public_bytes_raw()).hexdigest()
    public_sha256 = hashlib.sha256(public_raw).hexdigest()

    signed = {
        key: value
        for key, value in receipt.items()
        if key not in {"signer_key_id", "signature_ed25519_b64url"}
    }
    unsigned = {key: value for key, value in signed.items() if key != "receipt_sha256"}
    phase_hashes = receipt.get("phase_evidence_sha256")
    resource_chain = receipt.get("resource_ancestor_chain")
    hash_fields = (
        "package_sha256",
        "pre_foundation_authority_sha256",
        "foundation_apply_receipt_sha256",
        "project_ancestry_evidence_sha256",
        "project_ancestry_chain_sha256",
        "release_tree_sha256",
        "transaction_prefix_sha256",
        "authority_receipt_public_key_sha256",
        "authority_receipt_public_key_id",
        "credential_id_sha256",
        "executor_hosts_receipt_sha256",
        "receipt_sha256",
        "signer_key_id",
    )
    if (
        frozenset(receipt) != bootstrap._INSTALL_RECEIPT_FIELDS
        or receipt.get("schema") != bootstrap.BOOTSTRAP_RECEIPT_SCHEMA
        or receipt.get("release_revision") != revision
        or receipt.get("release_path") != str(layout.release_base / revision)
        or _REVISION.fullmatch(str(receipt.get("source_tree_oid", ""))) is None
        or any(
            _SHA256.fullmatch(str(receipt.get(field, ""))) is None
            for field in hash_fields
        )
        or type(receipt.get("installed_at_unix")) is not int
        or receipt["installed_at_unix"] <= 0
        or not isinstance(resource_chain, list)
        or not resource_chain
        or any(not isinstance(item, str) or not item for item in resource_chain)
        or not isinstance(phase_hashes, Mapping)
        or set(phase_hashes) != set(bootstrap.INSTALL_PHASES[:-1])
        or any(_SHA256.fullmatch(str(value)) is None for value in phase_hashes.values())
        or receipt.get("credential_id_sha256")
        != bootstrap.EXPECTED_CREDENTIAL_ID_SHA256
        or receipt.get("authority_receipt_public_key_sha256") != public_sha256
        or receipt.get("authority_receipt_public_key_id") != public_key_id
        or receipt.get("signer_key_id") != public_key_id
        or receipt.get("receipt_sha256") != foundation.sha256_json(unsigned)
        or receipt.get("systemd_units_enabled") != []
        or any(
            receipt.get(field) is not False
            for field in (
                "current_release_selected",
                "activation_performed",
                "activation_seal_created",
                "iam_binding_created",
                "cloud_mutation_performed",
                "caddy_cutover_performed",
            )
        )
    ):
        _error("owner_gate_runtime_install_receipt_invalid")
    try:
        signature = bootstrap._b64url(
            receipt.get("signature_ed25519_b64url"),
            maximum=64,
        )
        if len(signature) != 64:
            raise ValueError
        public_key.verify(signature, foundation.canonical_json_bytes(signed))
    except (
        bootstrap.OwnerGateBootstrapError,
        foundation.OwnerGateFoundationError,
        InvalidSignature,
        ValueError,
    ):
        _error("owner_gate_runtime_install_receipt_invalid")
    return receipt, hashlib.sha256(raw).hexdigest()


def _validate_release_and_unit_bytes(
    revision: str,
    receipt: Mapping[str, Any],
    *,
    layout: bootstrap.InstallLayout,
    expected_uid: int,
    expected_gid: int,
) -> Mapping[str, str]:
    release = layout.release_base / revision
    try:
        tree_sha256, node_count = bootstrap._predecessor_release_tree(
            release,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
    except (
        OSError,
        bootstrap.OwnerGateBootstrapError,
    ):
        _error("owner_gate_runtime_release_evidence_invalid")
    if node_count < len(INSTALLED_UNITS) or tree_sha256 != receipt.get(
        "release_tree_sha256"
    ):
        _error("owner_gate_runtime_release_evidence_invalid")
    for relative in REQUIRED_RUNTIME_ENTRYPOINTS:
        _read_owned_regular(
            release / relative,
            maximum=MAX_EVIDENCE_BYTES,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            modes=frozenset({0o555}),
            error_code="owner_gate_runtime_release_evidence_invalid",
        )

    unit_digests: dict[str, str] = {}
    for unit in INSTALLED_UNITS:
        source = _read_owned_regular(
            release / "ops/muncho/owner-gate" / unit,
            maximum=MAX_EVIDENCE_BYTES,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            modes=frozenset({0o444}),
            error_code="owner_gate_runtime_unit_bytes_invalid",
        )
        installed = _read_owned_regular(
            layout.systemd_root / unit,
            maximum=MAX_EVIDENCE_BYTES,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            modes=frozenset({0o444}),
            error_code="owner_gate_runtime_unit_bytes_invalid",
        )
        if source != installed:
            _error("owner_gate_runtime_unit_bytes_invalid")
        unit_digests[unit] = hashlib.sha256(source).hexdigest()
    return unit_digests


def _current_link_state(
    release: Path,
    *,
    layout: bootstrap.InstallLayout,
    expected_uid: int,
    expected_gid: int,
) -> str:
    try:
        before = layout.current_link.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        _error("owner_gate_runtime_current_link_invalid")
    if (
        not stat.S_ISLNK(before.st_mode)
        or before.st_uid != expected_uid
        or before.st_gid != expected_gid
    ):
        _error("owner_gate_runtime_current_link_invalid")
    try:
        target = os.readlink(layout.current_link)
        after = layout.current_link.lstat()
    except OSError:
        _error("owner_gate_runtime_current_link_invalid")
    if target != str(release) or (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ):
        _error("owner_gate_runtime_current_link_invalid")
    return "exact"


def _validate_common_unit_values(
    unit: str,
    values: Mapping[str, str],
    *,
    layout: bootstrap.InstallLayout,
) -> None:
    expected_common = {
        "Id": unit,
        "LoadState": "loaded",
        "FragmentPath": str(layout.systemd_root / unit),
        "DropInPaths": "",
        "NeedDaemonReload": "no",
    }
    if (
        not isinstance(values, Mapping)
        or frozenset(values) != frozenset(SYSTEMD_PROPERTIES)
        or any(values.get(key) != value for key, value in expected_common.items())
    ):
        _error("owner_gate_runtime_unit_state_invalid")


def _validate_inert_units(
    values: Mapping[str, Mapping[str, str]],
    *,
    layout: bootstrap.InstallLayout,
) -> None:
    if set(values) != set(INSTALLED_UNITS):
        _error("owner_gate_runtime_unit_state_invalid")
    for unit in INSTALLED_UNITS:
        observed = values[unit]
        _validate_common_unit_values(unit, observed, layout=layout)
        if (
            observed.get("ActiveState") != "inactive"
            or observed.get("SubState") != "dead"
            or observed.get("UnitFileState") != "disabled"
        ):
            _error("owner_gate_runtime_unit_state_invalid")


def _validate_ready_units(
    values: Mapping[str, Mapping[str, str]],
    *,
    layout: bootstrap.InstallLayout,
) -> None:
    if set(values) != set(READY_UNIT_STATES):
        _error("owner_gate_runtime_unit_state_invalid")
    for unit, (
        active_state,
        sub_state,
        unit_file_state,
    ) in READY_UNIT_STATES.items():
        observed = values[unit]
        _validate_common_unit_values(unit, observed, layout=layout)
        if (
            observed.get("ActiveState") != active_state
            or observed.get("SubState") != sub_state
            or observed.get("UnitFileState") != unit_file_state
        ):
            _error("owner_gate_runtime_unit_state_invalid")


def preflight_owner_gate_runtime(
    revision: str,
    *,
    layout: bootstrap.InstallLayout = bootstrap.PRODUCTION_LAYOUT,
    observer: SystemdStateObserver | None = None,
    expected_uid: int = 0,
    expected_gid: int = 0,
) -> Mapping[str, Any]:
    """Return one canonical read-only runtime classification."""

    if (
        not isinstance(revision, str)
        or _REVISION.fullmatch(revision) is None
        or not layout.release_base.is_absolute()
        or not layout.current_link.is_absolute()
        or not layout.etc_root.is_absolute()
        or not layout.state_root.is_absolute()
        or not layout.systemd_root.is_absolute()
        or ".."
        in (
            *layout.release_base.parts,
            *layout.current_link.parts,
            *layout.etc_root.parts,
            *layout.state_root.parts,
            *layout.systemd_root.parts,
        )
        or type(expected_uid) is not int
        or type(expected_gid) is not int
        or expected_uid < 0
        or expected_gid < 0
    ):
        _error("owner_gate_runtime_preflight_input_invalid")

    receipt, receipt_file_sha256 = _validate_install_receipt(
        revision,
        layout=layout,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    unit_digests = _validate_release_and_unit_bytes(
        revision,
        receipt,
        layout=layout,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    release = layout.release_base / revision
    link_state = _current_link_state(
        release,
        layout=layout,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    systemd = SystemdObserver() if observer is None else observer
    if link_state == "absent":
        activation_seal = layout.etc_root / foundation.MUTATION_ENABLE_SEAL.name
        if os.path.lexists(activation_seal):
            _error("owner_gate_runtime_activation_seal_present")
        observed = systemd.observe(INSTALLED_UNITS)
        _validate_inert_units(observed, layout=layout)
        state = "restore_required_inert"
        observed_units = INSTALLED_UNITS
    elif link_state == "exact":
        observed_units = INSTALLED_UNITS
        observed = systemd.observe(observed_units)
        _validate_ready_units(observed, layout=layout)
        state = "ready"
    else:  # Defensive exhaustiveness if the link classifier is extended.
        _error("owner_gate_runtime_current_link_invalid")

    observation = [{"unit": unit, **dict(observed[unit])} for unit in observed_units]
    unsigned = {
        "schema": PREFLIGHT_SCHEMA,
        "release_revision": revision,
        "release_path": str(release),
        "package_sha256": receipt["package_sha256"],
        "release_tree_sha256": receipt["release_tree_sha256"],
        "install_receipt_sha256": receipt["receipt_sha256"],
        "install_receipt_file_sha256": receipt_file_sha256,
        "authority_receipt_public_key_sha256": receipt[
            "authority_receipt_public_key_sha256"
        ],
        "installed_unit_bytes_sha256": unit_digests,
        "unit_observation_sha256": foundation.sha256_json(observation),
        "observed_units": list(observed_units),
        "current_link_state": link_state,
        "state": state,
        "read_only": True,
        "mutation_performed": False,
        "systemd_mutation_performed": False,
        "network_call_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "preflight_sha256": foundation.sha256_json(unsigned),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0 or os.getegid() != 0:
        parser.error("owner_gate_runtime_preflight_root_required")
    try:
        result = preflight_owner_gate_runtime(args.revision)
    except OwnerGateRuntimePreflightError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(foundation.canonical_json_bytes(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
