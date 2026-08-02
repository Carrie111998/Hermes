#!/usr/bin/env python3
"""Challenge-bound, read-only live readback for frozen owner-gate preparation.

The target recomputes installed host-runtime and signer-readiness facts.  It
does not decide whether activation is authorized, mutate state, or maintain a
replay ledger.  The owner independently validates the signed raw facts against
the frozen carrier lineage.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from scripts.canary import owner_gate_foundation as foundation
from scripts.canary import owner_gate_host_observation as host_observation
from scripts.canary import owner_gate_stage0 as stage0
from scripts.canary import owner_gate_trust as release_trust
from scripts.canary import storage_growth_trusted_collector as trusted
from scripts.canary import trusted_signer_provisioning as provisioning
from scripts.canary import trusted_signer_stage0 as host_runtime


REQUEST_SCHEMA = "muncho-owner-gate-preparation-readback-request.v2"
RESPONSE_SCHEMA = "muncho-owner-gate-preparation-readback-response.v1"
ATTESTATION_SCHEMA = (
    "muncho-owner-gate-preparation-readback-attestation.v1"
)
CARRIER_ENVELOPE_SCHEMA = (
    "muncho-owner-gate-host-preparation-carrier-envelope.v1"
)
CARRIER_SCHEMA = (
    "muncho-owner-gate-host-observation-preparation-carrier.v1"
)
PHASE = "post_iam"
OWNER_RELEASE_BASE = Path("/opt/muncho-owner-gate/releases")
MAX_FRAME_BYTES = 8 * 1024 * 1024

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_FIELDS = frozenset({
    "schema",
    "phase",
    "release_revision",
    "challenge_b64url",
    "carrier_sha256",
    "terminal_receipt_sha256",
    "inert_evidence_set_sha256",
    "iam_transaction_id",
    "carrier_envelope",
    "request_sha256",
})
_CARRIER_ENVELOPE_FIELDS = frozenset({
    "schema",
    "carrier",
    "carrier_sha256",
    "release_public_key_id",
    "signature_ed25519_b64url",
})
_CARRIER_FIELDS = frozenset({
    "schema",
    "captured_phase",
    "allowed_rehydrate_phase",
    "release_revision",
    "package_inventory_sha256",
    "input_pins",
    "binding",
    "foundation",
    "terminal_receipt_sha256",
    "terminal_artifact_sha256",
    "host_runtime_receipt",
    "cloud_signer_provisioning_receipt",
    "cloud_signer_provisioning_receipt_file_sha256",
    "cloud_signer_readiness",
    "host_signer_provisioning_receipt",
    "host_signer_provisioning_receipt_file_sha256",
    "host_signer_readiness",
    "freshness_asserted",
    "present_time_host_state_asserted",
    "present_time_iam_state_asserted",
    "prepared_under_iam_binding_present",
    "activation_performed",
    "cloud_mutation_performed",
    "service_activation_performed",
    "carrier_sha256",
})
_CARRIER_BINDING_FIELDS = frozenset({
    "source_tree_oid",
    "package_sha256",
    "package_inventory_sha256",
    "interpreter_sha256",
    "cloud_collector_public_key_id",
    "host_collector_public_key_id",
    "bootstrap_pip_version",
    "bootstrap_pip_sha256",
    "kit_release_id",
    "trusted_runner_path",
    "bundle_path",
})
_RESPONSE_FIELDS = frozenset({
    "schema",
    "phase",
    "release_revision",
    "challenge_b64url",
    "request_sha256",
    "carrier_sha256",
    "terminal_receipt_sha256",
    "inert_evidence_set_sha256",
    "iam_transaction_id",
    "package_manifest",
    "package_manifest_file_sha256",
    "install_receipt",
    "install_receipt_file_sha256",
    "host_runtime_receipt",
    "cloud_signer_readiness",
    "host_signer_readiness",
    "response_sha256",
    "attestation",
})
EXPECTED_LINEAGE_FIELDS = frozenset({
    "source_tree_oid",
    "package_sha256",
    "package_inventory_sha256",
    "install_receipt_sha256",
    "install_receipt_file_sha256",
    "host_runtime_receipt_sha256",
    "cloud_signer_provisioning_receipt_sha256",
    "cloud_signer_readiness_sha256",
    "host_signer_provisioning_receipt_sha256",
    "host_signer_readiness_sha256",
    "cloud_public_key_id",
})
_READINESS_FIELDS = frozenset({
    "schema",
    "role",
    "release_revision",
    "package_sha256",
    "public_key_id",
    "provisioning_receipt_sha256",
    "private_public_identity_matched",
    "config_exact",
    "replay_directory_exact",
    "sudoers_exact",
    "offline_runtime_exact",
    "activation_seal_absent",
    "current_link_absent",
    "services_inactive_disabled",
    "activation_performed",
    "iam_mutation_performed",
    "readiness_sha256",
})
_INSTALL_RECEIPT_FIELDS = frozenset({
    "schema",
    "release_revision",
    "package_sha256",
    "source_tree_oid",
    "pre_foundation_authority_sha256",
    "foundation_apply_receipt_sha256",
    "project_ancestry_evidence_sha256",
    "project_ancestry_chain_sha256",
    "resource_ancestor_chain",
    "installed_at_unix",
    "release_path",
    "release_tree_sha256",
    "transaction_prefix_sha256",
    "phase_evidence_sha256",
    "authority_receipt_public_key_sha256",
    "authority_receipt_public_key_id",
    "credential_id_sha256",
    "executor_hosts_receipt_sha256",
    "current_release_selected",
    "systemd_units_enabled",
    "activation_performed",
    "activation_seal_created",
    "iam_binding_created",
    "cloud_mutation_performed",
    "caddy_cutover_performed",
    "receipt_sha256",
    "signer_key_id",
    "signature_ed25519_b64url",
})
_INSTALL_LINEAGE_FIELDS = (
    "pre_foundation_authority_sha256",
    "foundation_apply_receipt_sha256",
    "project_ancestry_evidence_sha256",
    "project_ancestry_chain_sha256",
)


class OwnerGatePreparationReadbackError(RuntimeError):
    """Stable, secret-free preparation readback failure."""


def _error(code: str, exc: BaseException | None = None) -> None:
    del exc
    raise OwnerGatePreparationReadbackError(code) from None


def _canonical(value: Any) -> bytes:
    try:
        return foundation.canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        _error("owner_gate_preparation_readback_json_invalid", exc)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _is_revision(value: Any) -> bool:
    return type(value) is str and _REVISION.fullmatch(value) is not None


def _is_sha256(value: Any) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _decode_canonical(
    raw: bytes,
    *,
    maximum: int = MAX_FRAME_BYTES,
    code: str = "owner_gate_preparation_readback_json_invalid",
) -> Mapping[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _error(code)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name, item in items:
            if name in value:
                raise ValueError("duplicate")
            value[name] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _item: (_ for _ in ()).throw(ValueError()),
            parse_float=lambda _item: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _error(code, exc)
    if not isinstance(value, Mapping) or _canonical(value) != raw:
        _error(code)
    return dict(value)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: Any, *, size: int, code: str) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        _error(code)
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (TypeError, ValueError) as exc:
        _error(code, exc)
    if len(raw) != size or _b64url_encode(raw) != value:
        _error(code)
    return raw


def _validate_host_runtime_receipt(
    value: Any,
    *,
    release_revision: str,
) -> Mapping[str, Any]:
    false_fields = (
        "network_install_required",
        "generic_usr_bin_python3_runtime",
        "service_start_performed",
        "service_enablement_mutated",
        "iam_mutation_performed",
        "cloud_mutation_performed",
        "private_key_material_received",
        "private_key_digest_recorded",
    )
    release = value.get("release") if isinstance(value, Mapping) else None
    sudoers = value.get("sudoers") if isinstance(value, Mapping) else None
    unsigned = (
        {
            name: item
            for name, item in value.items()
            if name != "receipt_sha256"
        }
        if isinstance(value, Mapping)
        else {}
    )
    expected_release = host_runtime.HOST_RELEASE_BASE / release_revision
    if (
        not isinstance(value, Mapping)
        or frozenset(value) != host_runtime.HOST_RUNTIME_RECEIPT_FIELDS
        or value.get("schema") != host_runtime.HOST_RUNTIME_RECEIPT_SCHEMA
        or value.get("release_revision") != release_revision
        or not _is_sha256(value.get("package_sha256"))
        or not _is_sha256(value.get("preflight_sha256"))
        or not _is_sha256(value.get("runtime_inventory_sha256"))
        or not isinstance(release, Mapping)
        or frozenset(release)
        != {
            "path",
            "uid",
            "gid",
            "mode",
            "projection_sha256",
            "projection_count",
        }
        or release.get("path") != str(expected_release)
        or release.get("uid") != 0
        or release.get("gid") != 0
        or release.get("mode") != "0555"
        or not _is_sha256(release.get("projection_sha256"))
        or type(release.get("projection_count")) is not int
        or release["projection_count"] < 1
        or not isinstance(sudoers, Mapping)
        or frozenset(sudoers) != {"path", "uid", "gid", "mode", "sha256"}
        or sudoers.get("path") != str(host_runtime.HOST_SUDOERS_PATH)
        or sudoers.get("uid") != 0
        or sudoers.get("gid") != 0
        or sudoers.get("mode") != "0440"
        or not _is_sha256(sudoers.get("sha256"))
        or value.get("runtime_interpreter")
        != str(expected_release / "venv/bin/python")
        or value.get("host_attestor_entrypoint")
        != str(expected_release / "bin/muncho-host-observation-attestor")
        or value.get("host_provisioner_entrypoint")
        != str(
            expected_release / "bin/muncho-host-trusted-signer-provision"
        )
        or value.get("offline_runtime") is not True
        or value.get("current_link_absent") is not True
        or value.get("activation_seal_absent") is not True
        or any(value.get(name) is not False for name in false_fields)
        or value.get("receipt_sha256") != _sha(unsigned)
    ):
        _error("owner_gate_preparation_readback_host_runtime_invalid")
    return dict(value)


def _validate_carrier_envelope_structure(
    value: Any,
    *,
    release_revision: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    """Validate exact carrier shape without treating caller bytes as authority."""

    if (
        not isinstance(value, Mapping)
        or frozenset(value) != _CARRIER_ENVELOPE_FIELDS
        or value.get("schema") != CARRIER_ENVELOPE_SCHEMA
        or not isinstance(value.get("carrier"), Mapping)
    ):
        _error("owner_gate_preparation_readback_carrier_invalid")
    envelope = dict(value)
    carrier = dict(envelope["carrier"])
    binding = carrier.get("binding")
    false_fields = (
        "freshness_asserted",
        "present_time_host_state_asserted",
        "present_time_iam_state_asserted",
        "prepared_under_iam_binding_present",
        "activation_performed",
        "cloud_mutation_performed",
        "service_activation_performed",
    )
    if (
        frozenset(carrier) != _CARRIER_FIELDS
        or carrier.get("schema") != CARRIER_SCHEMA
        or carrier.get("captured_phase") != "inert"
        or carrier.get("allowed_rehydrate_phase") != PHASE
        or carrier.get("release_revision") != release_revision
        or not _is_sha256(carrier.get("package_inventory_sha256"))
        or not isinstance(carrier.get("input_pins"), Mapping)
        or not isinstance(carrier.get("foundation"), Mapping)
        or not isinstance(binding, Mapping)
        or frozenset(binding) != _CARRIER_BINDING_FIELDS
        or not _is_revision(binding.get("source_tree_oid"))
        or any(
            not _is_sha256(binding.get(name))
            for name in (
                "package_sha256",
                "package_inventory_sha256",
                "interpreter_sha256",
                "cloud_collector_public_key_id",
                "host_collector_public_key_id",
                "bootstrap_pip_sha256",
                "kit_release_id",
            )
        )
        or binding.get("package_inventory_sha256")
        != carrier.get("package_inventory_sha256")
        or not isinstance(binding.get("bootstrap_pip_version"), str)
        or not binding["bootstrap_pip_version"]
        or not isinstance(binding.get("trusted_runner_path"), str)
        or not binding["trusted_runner_path"]
        or not isinstance(binding.get("bundle_path"), str)
        or not binding["bundle_path"]
        or any(
            not _is_sha256(carrier.get(name))
            for name in (
                "terminal_receipt_sha256",
                "terminal_artifact_sha256",
                "cloud_signer_provisioning_receipt_file_sha256",
                "host_signer_provisioning_receipt_file_sha256",
                "carrier_sha256",
            )
        )
        or any(
            not isinstance(carrier.get(name), Mapping)
            for name in (
                "cloud_signer_provisioning_receipt",
                "cloud_signer_readiness",
                "host_signer_provisioning_receipt",
                "host_signer_readiness",
            )
        )
        or carrier.get("carrier_sha256")
        != _sha({
            name: item
            for name, item in carrier.items()
            if name != "carrier_sha256"
        })
        or any(carrier.get(name) is not False for name in false_fields)
        or envelope.get("carrier_sha256")
        != carrier.get("carrier_sha256")
        or not _is_sha256(envelope.get("release_public_key_id"))
        or _b64url_decode(
            envelope.get("signature_ed25519_b64url"),
            size=64,
            code="owner_gate_preparation_readback_carrier_invalid",
        )
        is None
    ):
        _error("owner_gate_preparation_readback_carrier_invalid")
    runtime = _validate_host_runtime_receipt(
        carrier.get("host_runtime_receipt"),
        release_revision=release_revision,
    )
    if runtime["package_sha256"] != binding["package_sha256"]:
        _error("owner_gate_preparation_readback_carrier_invalid")
    return envelope, carrier, runtime


def _validate_release_signed_carrier_envelope(
    value: Any,
    *,
    release_revision: str,
    release_public_key: Ed25519PublicKey,
    package_sha256: str,
    package_inventory_sha256: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    """Authenticate the frozen carrier before any installed host code runs."""

    envelope, carrier, runtime = _validate_carrier_envelope_structure(
        value,
        release_revision=release_revision,
    )
    binding = carrier["binding"]
    key_id = (
        hashlib.sha256(release_public_key.public_bytes_raw()).hexdigest()
        if isinstance(release_public_key, Ed25519PublicKey)
        else ""
    )
    signed = {
        name: item
        for name, item in envelope.items()
        if name != "signature_ed25519_b64url"
    }
    if (
        not isinstance(release_public_key, Ed25519PublicKey)
        or envelope.get("release_public_key_id") != key_id
        or binding.get("package_sha256") != package_sha256
        or carrier.get("package_inventory_sha256")
        != package_inventory_sha256
    ):
        _error("owner_gate_preparation_readback_carrier_invalid")
    try:
        release_public_key.verify(
            _b64url_decode(
                envelope["signature_ed25519_b64url"],
                size=64,
                code="owner_gate_preparation_readback_carrier_invalid",
            ),
            _canonical(signed),
        )
    except InvalidSignature as exc:
        _error("owner_gate_preparation_readback_carrier_invalid", exc)
    return envelope, carrier, runtime


def _load_release_carrier_public_key(
    *,
    release_revision: str,
    package: Mapping[str, Any],
) -> Ed25519PublicKey:
    path = (
        OWNER_RELEASE_BASE
        / release_revision
        / "trust/release-trust-signing.pub"
    )
    try:
        raw = release_trust._read_immutable(
            path,
            maximum=32,
            expected_uid=0,
            allowed_modes=frozenset({0o444}),
        )
        key = Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, release_trust.OwnerGateTrustError) as exc:
        _error("owner_gate_preparation_readback_carrier_key_invalid", exc)
    digest = hashlib.sha256(raw).hexdigest()
    if (
        digest != package.get("trust_public_key_sha256")
        or digest != release_trust.PINNED_RELEASE_TRUST_PUBLIC_KEY_SHA256
    ):
        _error("owner_gate_preparation_readback_carrier_key_invalid")
    return key


def _validate_request(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != _REQUEST_FIELDS:
        _error("owner_gate_preparation_readback_request_invalid")
    request = dict(value)
    release_revision = request.get("release_revision")
    unsigned = {
        name: item
        for name, item in request.items()
        if name != "request_sha256"
    }
    if (
        request.get("schema") != REQUEST_SCHEMA
        or request.get("phase") != PHASE
        or not _is_revision(release_revision)
        or any(
            not _is_sha256(request.get(name))
            for name in (
                "carrier_sha256",
                "terminal_receipt_sha256",
                "inert_evidence_set_sha256",
                "iam_transaction_id",
            )
        )
        or request.get("request_sha256") != _sha(unsigned)
    ):
        _error("owner_gate_preparation_readback_request_invalid")
    _b64url_decode(
        request.get("challenge_b64url"),
        size=32,
        code="owner_gate_preparation_readback_challenge_invalid",
    )
    envelope, carrier, _runtime = _validate_carrier_envelope_structure(
        request.get("carrier_envelope"),
        release_revision=release_revision,
    )
    if (
        request.get("carrier_sha256") != envelope["carrier_sha256"]
        or request.get("terminal_receipt_sha256")
        != carrier["terminal_receipt_sha256"]
    ):
        _error("owner_gate_preparation_readback_request_invalid")
    return request


def build_preparation_readback_request(
    *,
    release_revision: str,
    carrier_envelope: Mapping[str, Any],
    release_public_key: Ed25519PublicKey,
    inert_evidence_set_sha256: str,
    iam_transaction_id: str,
) -> Mapping[str, Any]:
    """Create one fresh challenge-bound owner request."""

    challenge = secrets.token_bytes(32)
    if len(challenge) != 32:
        _error("owner_gate_preparation_readback_challenge_invalid")
    _envelope, carrier, _runtime = (
        _validate_carrier_envelope_structure(
            carrier_envelope,
            release_revision=release_revision,
        )
    )
    binding = carrier["binding"]
    checked_envelope, checked_carrier, _checked_runtime = (
        _validate_release_signed_carrier_envelope(
            carrier_envelope,
            release_revision=release_revision,
            release_public_key=release_public_key,
            package_sha256=str(binding["package_sha256"]),
            package_inventory_sha256=str(
                carrier["package_inventory_sha256"]
            ),
        )
    )
    unsigned = {
        "schema": REQUEST_SCHEMA,
        "phase": PHASE,
        "release_revision": release_revision,
        "challenge_b64url": _b64url_encode(challenge),
        "carrier_sha256": checked_envelope["carrier_sha256"],
        "terminal_receipt_sha256": checked_carrier[
            "terminal_receipt_sha256"
        ],
        "inert_evidence_set_sha256": inert_evidence_set_sha256,
        "iam_transaction_id": iam_transaction_id,
        "carrier_envelope": checked_envelope,
    }
    return _validate_request({
        **unsigned,
        "request_sha256": _sha(unsigned),
    })


def _validate_package_manifest(
    value: Any,
    *,
    release_revision: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error("owner_gate_preparation_readback_package_invalid")
    manifest = dict(value)
    unsigned = {
        name: item
        for name, item in manifest.items()
        if name != "package_sha256"
    }
    try:
        inventory = {
            name: manifest[name]
            for name in stage0.INVENTORY_FIELDS
        }
    except KeyError:
        _error("owner_gate_preparation_readback_package_invalid")
    if (
        frozenset(manifest) != stage0.MANIFEST_FIELDS
        or manifest.get("schema") != stage0.PACKAGE_SCHEMA
        or manifest.get("release_revision") != release_revision
        or manifest.get("release_root")
        != str(OWNER_RELEASE_BASE / release_revision)
        or manifest.get("package_sha256") != _sha(unsigned)
        or manifest.get("package_inventory_sha256") != _sha(inventory)
        or manifest.get("activation_performed") is not False
        or manifest.get("cloud_mutation_performed") is not False
        or manifest.get("generic_shell_entrypoint") is not False
        or manifest.get("local_gcloud_runtime_fallback") is not False
        or manifest.get("secret_material_recorded") is not False
        or manifest.get("secret_digest_recorded") is not False
    ):
        _error("owner_gate_preparation_readback_package_invalid")
    return manifest


def _load_current_package_and_install(
    release_revision: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any], bytes]:
    try:
        package = host_observation._load_release_package(release_revision)
        path = (
            host_observation.INSTALL_RECEIPT_BASE
            / f"install-{release_revision}.json"
        )
        raw = host_observation._read_regular(
            path,
            maximum=host_observation.MAX_FILE_BYTES,
            modes=frozenset({0o400}),
        )
        receipt = host_observation._decode_canonical(
            raw,
            maximum=host_observation.MAX_FILE_BYTES,
            code="owner_gate_host_install_receipt_invalid",
        )
        install = host_observation._validate_install_receipt(
            receipt,
            package=package,
        )
    except host_observation.OwnerGateHostObservationError as exc:
        _error("owner_gate_preparation_readback_install_invalid", exc)
    return dict(package), dict(install), raw


def _validate_readiness(
    value: Any,
    *,
    role: str,
    release_revision: str,
    package_sha256: str,
    public_key_id: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error("owner_gate_preparation_readback_readiness_invalid")
    readiness = dict(value)
    unsigned = {
        name: item
        for name, item in readiness.items()
        if name != "readiness_sha256"
    }
    if (
        role not in {"cloud", "host"}
        or frozenset(readiness) != _READINESS_FIELDS
        or readiness.get("schema") != provisioning.READINESS_SCHEMA
        or readiness.get("role") != role
        or readiness.get("release_revision") != release_revision
        or readiness.get("package_sha256") != package_sha256
        or readiness.get("public_key_id") != public_key_id
        or not _is_sha256(
            readiness.get("provisioning_receipt_sha256")
        )
        or any(
            readiness.get(name) is not True
            for name in (
                "private_public_identity_matched",
                "config_exact",
                "replay_directory_exact",
                "sudoers_exact",
                "offline_runtime_exact",
                "activation_seal_absent",
                "current_link_absent",
                "services_inactive_disabled",
            )
        )
        or readiness.get("activation_performed") is not False
        or readiness.get("iam_mutation_performed") is not False
        or readiness.get("readiness_sha256") != _sha(unsigned)
    ):
        _error("owner_gate_preparation_readback_readiness_invalid")
    return readiness


def _load_cloud_signer(
    *,
    expected_public_key_id: str,
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey, str]:
    try:
        config = trusted.load_cloud_attestor_config()
        private_key, public_key, public_key_id = trusted._load_private_key(
            config
        )
    except trusted.TrustedObservationError as exc:
        _error("owner_gate_preparation_readback_signer_unavailable", exc)
    if public_key_id != expected_public_key_id:
        _error("owner_gate_preparation_readback_signer_invalid")
    return private_key, public_key, public_key_id


def _collect_validated_live_state(
    request: Mapping[str, Any],
    *,
    release_revision: str,
) -> Mapping[str, Any]:
    """Capture one fully validated live-state projection.

    The caller captures this projection twice around signer-key loading and
    requires byte equality.  The second projection is therefore the terminal
    current-state fence immediately before the response is signed.
    """

    package, install, install_raw = _load_current_package_and_install(
        release_revision
    )
    package = _validate_package_manifest(
        package,
        release_revision=release_revision,
    )
    release_public_key = _load_release_carrier_public_key(
        release_revision=release_revision,
        package=package,
    )
    _carrier_envelope, carrier, expected_host_runtime = (
        _validate_release_signed_carrier_envelope(
            request["carrier_envelope"],
            release_revision=release_revision,
            release_public_key=release_public_key,
            package_sha256=str(package["package_sha256"]),
            package_inventory_sha256=str(
                package["package_inventory_sha256"]
            ),
        )
    )
    if (
        request["carrier_sha256"]
        != request["carrier_envelope"]["carrier_sha256"]
        or request["terminal_receipt_sha256"]
        != carrier["terminal_receipt_sha256"]
    ):
        _error("owner_gate_preparation_readback_lineage_invalid")
    expected_host_runtime = _validate_host_runtime_receipt(
        expected_host_runtime,
        release_revision=release_revision,
    )
    if expected_host_runtime["package_sha256"] != package["package_sha256"]:
        _error("owner_gate_preparation_readback_lineage_invalid")
    try:
        checked_host_runtime = host_runtime.verify_host_offline_runtime(
            release_revision,
            expected_receipt=expected_host_runtime,
        )
        cloud_readiness = provisioning.verify_cloud_signer_inert_readiness(
            release_revision
        )
        host_readiness = provisioning.verify_host_signer_runtime_readiness(
            release_revision
        )
    except (
        host_runtime.TrustedSignerStage0Error,
        provisioning.TrustedSignerProvisioningError,
    ) as exc:
        _error("owner_gate_preparation_readback_live_state_invalid", exc)
    collectors = package.get("collector_public_key_ids")
    if (
        not isinstance(collectors, Mapping)
        or frozenset(collectors) != {"network", "cloud", "host"}
    ):
        _error("owner_gate_preparation_readback_package_invalid")
    cloud_readiness = _validate_readiness(
        cloud_readiness,
        role="cloud",
        release_revision=release_revision,
        package_sha256=str(package["package_sha256"]),
        public_key_id=str(collectors["cloud"]),
    )
    host_readiness = _validate_readiness(
        host_readiness,
        role="host",
        release_revision=release_revision,
        package_sha256=str(package["package_sha256"]),
        public_key_id=str(collectors["host"]),
    )
    return {
        "package_manifest": package,
        "package_manifest_file_sha256": hashlib.sha256(
            _canonical(package)
        ).hexdigest(),
        "install_receipt": install,
        "install_receipt_file_sha256": hashlib.sha256(
            install_raw
        ).hexdigest(),
        "host_runtime_receipt": checked_host_runtime,
        "cloud_signer_readiness": cloud_readiness,
        "host_signer_readiness": host_readiness,
    }


def collect_preparation_readback(
    request_value: Mapping[str, Any],
    *,
    release_revision: str,
) -> Mapping[str, Any]:
    """Recompute, sign, and return raw preparation facts without mutation."""

    if (
        os.geteuid() != 0  # windows-footgun: ok — Debian root boundary
        or not _is_revision(release_revision)
    ):
        _error("owner_gate_preparation_readback_runtime_invalid")
    request = _validate_request(request_value)
    if request["release_revision"] != release_revision:
        _error("owner_gate_preparation_readback_runtime_invalid")
    initial = _collect_validated_live_state(
        request,
        release_revision=release_revision,
    )
    package = initial["package_manifest"]
    collectors = package["collector_public_key_ids"]
    private_key, public_key, public_key_id = _load_cloud_signer(
        expected_public_key_id=str(collectors["cloud"]),
    )
    terminal = _collect_validated_live_state(
        request,
        release_revision=release_revision,
    )
    if _canonical(terminal) != _canonical(initial):
        _error("owner_gate_preparation_readback_live_state_changed")
    unsigned = {
        "schema": RESPONSE_SCHEMA,
        "phase": PHASE,
        "release_revision": release_revision,
        "challenge_b64url": request["challenge_b64url"],
        "request_sha256": request["request_sha256"],
        "carrier_sha256": request["carrier_sha256"],
        "terminal_receipt_sha256": request["terminal_receipt_sha256"],
        "inert_evidence_set_sha256": request[
            "inert_evidence_set_sha256"
        ],
        "iam_transaction_id": request["iam_transaction_id"],
        **terminal,
    }
    report = {
        **unsigned,
        "response_sha256": _sha(unsigned),
    }
    signature = private_key.sign(_canonical(report))
    response = {
        **report,
        "attestation": {
            "schema": ATTESTATION_SCHEMA,
            "public_key_id": public_key_id,
            "signature_ed25519_b64url": _b64url_encode(signature),
        },
    }
    try:
        public_key.verify(signature, _canonical(report))
    except InvalidSignature as exc:  # pragma: no cover - defensive
        _error("owner_gate_preparation_readback_signer_invalid", exc)
    return response


def _validate_install_receipt_facts(
    value: Any,
    *,
    package: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error("owner_gate_preparation_readback_install_invalid")
    receipt = dict(value)
    signed = {
        name: item
        for name, item in receipt.items()
        if name not in {"signer_key_id", "signature_ed25519_b64url"}
    }
    unsigned = {
        name: item
        for name, item in signed.items()
        if name != "receipt_sha256"
    }
    false_fields = (
        "current_release_selected",
        "activation_performed",
        "activation_seal_created",
        "iam_binding_created",
        "cloud_mutation_performed",
        "caddy_cutover_performed",
    )
    if (
        frozenset(receipt) != _INSTALL_RECEIPT_FIELDS
        or receipt.get("schema")
        != "muncho-owner-gate-offline-install-receipt.v1"
        or receipt.get("release_revision")
        != package.get("release_revision")
        or receipt.get("source_tree_oid") != package.get("source_tree_oid")
        or receipt.get("package_sha256") != package.get("package_sha256")
        or any(
            receipt.get(name) != package.get(name)
            for name in _INSTALL_LINEAGE_FIELDS
        )
        or receipt.get("resource_ancestor_chain")
        != package.get("resource_ancestor_chain")
        or receipt.get("release_path")
        != str(
            OWNER_RELEASE_BASE / str(package.get("release_revision", ""))
        )
        or receipt.get("systemd_units_enabled") != []
        or any(receipt.get(name) is not False for name in false_fields)
        or receipt.get("receipt_sha256") != _sha(unsigned)
        or receipt.get("signer_key_id")
        != receipt.get("authority_receipt_public_key_id")
        or not _is_sha256(
            receipt.get("authority_receipt_public_key_id")
        )
        or _b64url_decode(
            receipt.get("signature_ed25519_b64url"),
            size=64,
            code="owner_gate_preparation_readback_install_invalid",
        )
        is None
    ):
        _error("owner_gate_preparation_readback_install_invalid")
    return receipt


def validate_preparation_readback_response(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    cloud_public_key: Ed25519PublicKey,
    expected_lineage: Mapping[str, str],
) -> Mapping[str, Any]:
    """Independently verify target signature, challenge, and frozen lineage."""

    checked_request = _validate_request(request)
    if (
        not isinstance(value, Mapping)
        or frozenset(value) != _RESPONSE_FIELDS
        or not isinstance(cloud_public_key, Ed25519PublicKey)
        or not isinstance(expected_lineage, Mapping)
        or frozenset(expected_lineage) != EXPECTED_LINEAGE_FIELDS
        or not _is_revision(expected_lineage.get("source_tree_oid"))
        or any(
            not _is_sha256(expected_lineage.get(name))
            for name in EXPECTED_LINEAGE_FIELDS - {"source_tree_oid"}
        )
    ):
        _error("owner_gate_preparation_readback_response_invalid")
    response = dict(value)
    attestation = response.get("attestation")
    report = {
        name: item
        for name, item in response.items()
        if name != "attestation"
    }
    unsigned = {
        name: item
        for name, item in report.items()
        if name != "response_sha256"
    }
    public_key_id = hashlib.sha256(
        cloud_public_key.public_bytes_raw()
    ).hexdigest()
    copied = (
        "phase",
        "release_revision",
        "challenge_b64url",
        "request_sha256",
        "carrier_sha256",
        "terminal_receipt_sha256",
        "inert_evidence_set_sha256",
        "iam_transaction_id",
    )
    if (
        response.get("schema") != RESPONSE_SCHEMA
        or any(
            response.get(name) != checked_request.get(name)
            for name in copied
        )
        or response.get("response_sha256") != _sha(unsigned)
        or not isinstance(attestation, Mapping)
        or frozenset(attestation)
        != {"schema", "public_key_id", "signature_ed25519_b64url"}
        or attestation.get("schema") != ATTESTATION_SCHEMA
        or attestation.get("public_key_id") != public_key_id
        or public_key_id != expected_lineage["cloud_public_key_id"]
    ):
        _error("owner_gate_preparation_readback_response_invalid")
    try:
        cloud_public_key.verify(
            _b64url_decode(
                attestation["signature_ed25519_b64url"],
                size=64,
                code="owner_gate_preparation_readback_response_invalid",
            ),
            _canonical(report),
        )
    except InvalidSignature as exc:
        _error("owner_gate_preparation_readback_response_invalid", exc)
    package = _validate_package_manifest(
        response.get("package_manifest"),
        release_revision=str(checked_request["release_revision"]),
    )
    if (
        response.get("package_manifest_file_sha256")
        != hashlib.sha256(_canonical(package)).hexdigest()
        or package.get("source_tree_oid")
        != expected_lineage["source_tree_oid"]
        or package.get("package_sha256")
        != expected_lineage["package_sha256"]
        or package.get("package_inventory_sha256")
        != expected_lineage["package_inventory_sha256"]
        or package.get("collector_public_key_ids", {}).get("cloud")
        != public_key_id
    ):
        _error("owner_gate_preparation_readback_lineage_invalid")
    install = _validate_install_receipt_facts(
        response.get("install_receipt"),
        package=package,
    )
    if (
        install.get("receipt_sha256")
        != expected_lineage["install_receipt_sha256"]
        or response.get("install_receipt_file_sha256")
        != expected_lineage["install_receipt_file_sha256"]
        or response.get("install_receipt_file_sha256")
        != hashlib.sha256(_canonical(install)).hexdigest()
    ):
        _error("owner_gate_preparation_readback_lineage_invalid")
    expected_host_runtime = _validate_host_runtime_receipt(
        checked_request["carrier_envelope"]["carrier"][
            "host_runtime_receipt"
        ],
        release_revision=str(checked_request["release_revision"]),
    )
    observed_host_runtime = _validate_host_runtime_receipt(
        response.get("host_runtime_receipt"),
        release_revision=str(checked_request["release_revision"]),
    )
    if (
        expected_host_runtime["receipt_sha256"]
        != expected_lineage["host_runtime_receipt_sha256"]
        or _canonical(observed_host_runtime)
        != _canonical(expected_host_runtime)
    ):
        _error("owner_gate_preparation_readback_lineage_invalid")
    collectors = package["collector_public_key_ids"]
    cloud_readiness = _validate_readiness(
        response.get("cloud_signer_readiness"),
        role="cloud",
        release_revision=str(checked_request["release_revision"]),
        package_sha256=str(package["package_sha256"]),
        public_key_id=str(collectors["cloud"]),
    )
    host_readiness = _validate_readiness(
        response.get("host_signer_readiness"),
        role="host",
        release_revision=str(checked_request["release_revision"]),
        package_sha256=str(package["package_sha256"]),
        public_key_id=str(collectors["host"]),
    )
    if (
        cloud_readiness["provisioning_receipt_sha256"]
        != expected_lineage[
            "cloud_signer_provisioning_receipt_sha256"
        ]
        or cloud_readiness["readiness_sha256"]
        != expected_lineage["cloud_signer_readiness_sha256"]
        or host_readiness["provisioning_receipt_sha256"]
        != expected_lineage[
            "host_signer_provisioning_receipt_sha256"
        ]
        or host_readiness["readiness_sha256"]
        != expected_lineage["host_signer_readiness_sha256"]
    ):
        _error("owner_gate_preparation_readback_lineage_invalid")
    return response


def _runtime_revision() -> str:
    try:
        return provisioning._runtime_release_revision(
            expected_base=OWNER_RELEASE_BASE
        )
    except provisioning.TrustedSignerProvisioningError as exc:
        _error("owner_gate_preparation_readback_runtime_invalid", exc)


def _read_stdin(stream: BinaryIO) -> Mapping[str, Any]:
    raw = bytearray(stream.read(MAX_FRAME_BYTES + 2))
    try:
        if (
            not raw
            or len(raw) > MAX_FRAME_BYTES + 1
            or raw[-1:] != b"\n"
            or b"\n" in raw[:-1]
        ):
            _error("owner_gate_preparation_readback_stdin_invalid")
        return _decode_canonical(
            bytes(raw[:-1]),
            code="owner_gate_preparation_readback_stdin_invalid",
        )
    finally:
        for index in range(len(raw)):
            raw[index] = 0


def _write_stdout(stream: BinaryIO, value: Mapping[str, Any]) -> None:
    raw = _canonical(value) + b"\n"
    if len(raw) > MAX_FRAME_BYTES + 1:
        _error("owner_gate_preparation_readback_stdout_invalid")
    stream.write(raw)
    stream.flush()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments:
        _error("owner_gate_preparation_readback_argv_invalid")
    release_revision = _runtime_revision()
    request = _read_stdin(sys.stdin.buffer)
    response = collect_preparation_readback(
        request,
        release_revision=release_revision,
    )
    _write_stdout(sys.stdout.buffer, response)
    return 0


__all__ = [
    "ATTESTATION_SCHEMA",
    "EXPECTED_LINEAGE_FIELDS",
    "OwnerGatePreparationReadbackError",
    "PHASE",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "build_preparation_readback_request",
    "collect_preparation_readback",
    "main",
    "validate_preparation_readback_response",
]
