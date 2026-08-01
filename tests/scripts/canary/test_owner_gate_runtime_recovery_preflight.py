from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.canary import owner_gate_bootstrap as bootstrap
from scripts.canary import owner_gate_foundation as foundation
from scripts.canary import owner_gate_runtime_recovery_preflight as preflight


REVISION = "f763c14211055bb4ef4db850c9a31755ab41f06a"
UID = os.geteuid()
GID = os.getegid()


def _write(path: Path, raw: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


def _seal_directories(root: Path) -> None:
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        path.chmod(0o555)
    root.chmod(0o555)


def _unit_values(
    unit: str,
    *,
    layout: bootstrap.InstallLayout,
    active_state: str,
    sub_state: str,
    unit_file_state: str,
) -> dict[str, str]:
    return {
        "Id": unit,
        "LoadState": "loaded",
        "ActiveState": active_state,
        "SubState": sub_state,
        "UnitFileState": unit_file_state,
        "FragmentPath": str(layout.systemd_root / unit),
        "DropInPaths": "",
        "NeedDaemonReload": "no",
    }


def _inert_states(
    layout: bootstrap.InstallLayout,
) -> dict[str, dict[str, str]]:
    return {
        unit: _unit_values(
            unit,
            layout=layout,
            active_state="inactive",
            sub_state="dead",
            unit_file_state="disabled",
        )
        for unit in preflight.INSTALLED_UNITS
    }


def _ready_states(
    layout: bootstrap.InstallLayout,
) -> dict[str, dict[str, str]]:
    states = {
        unit: _unit_values(
            unit,
            layout=layout,
            active_state=active,
            sub_state=sub,
            unit_file_state=unit_file_state,
        )
        for unit, (
            active,
            sub,
            unit_file_state,
        ) in preflight.READY_UNIT_STATES.items()
    }
    return states


class RecordingObserver:
    def __init__(self, values: Mapping[str, Mapping[str, str]]) -> None:
        self.values = values
        self.calls: list[tuple[str, ...]] = []

    def observe(self, units: Sequence[str]) -> Mapping[str, Mapping[str, str]]:
        requested = tuple(units)
        self.calls.append(requested)
        return {unit: dict(self.values[unit]) for unit in requested}


def _layout(tmp_path: Path) -> bootstrap.InstallLayout:
    return bootstrap.InstallLayout(
        release_base=tmp_path / "opt/muncho-owner-gate/releases",
        current_link=tmp_path / "opt/muncho-owner-gate/current",
        etc_root=tmp_path / "etc/muncho-owner-gate",
        state_root=tmp_path / "var/lib/muncho-owner-gate",
        systemd_root=tmp_path / "etc/systemd/system",
    )


def _install_trusted_release(
    layout: bootstrap.InstallLayout,
) -> tuple[Path, dict[str, Any]]:
    release = layout.release_base / REVISION
    for index, unit in enumerate(preflight.INSTALLED_UNITS):
        raw = f"[Unit]\nDescription={unit}:{index}\n".encode("ascii")
        _write(
            release / "ops/muncho/owner-gate" / unit,
            raw,
            mode=0o444,
        )
        _write(layout.systemd_root / unit, raw, mode=0o444)
    _write(
        release / "bin/muncho-passkey-v2-authority",
        b"#!/bin/sh\nexit 0\n",
        mode=0o555,
    )
    _write(
        release / "bin/muncho-passkey-v2-web",
        b"#!/bin/sh\nexit 0\n",
        mode=0o555,
    )
    _write(
        release / "bin/muncho-passkey-v2-executor",
        b"#!/bin/sh\nexit 0\n",
        mode=0o555,
    )
    _seal_directories(release)
    tree_sha256, _node_count = bootstrap._predecessor_release_tree(
        release,
        expected_uid=UID,
        expected_gid=GID,
    )

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_raw = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key_id = hashlib.sha256(public_key.public_bytes_raw()).hexdigest()
    _write(
        layout.etc_root / "public/authority-receipt-public.pem",
        public_raw,
        mode=0o444,
    )

    phase_hashes = {
        phase: hashlib.sha256(phase.encode("ascii")).hexdigest()
        for phase in bootstrap.INSTALL_PHASES[:-1]
    }
    unsigned: dict[str, Any] = {
        "schema": bootstrap.BOOTSTRAP_RECEIPT_SCHEMA,
        "release_revision": REVISION,
        "package_sha256": "1" * 64,
        "source_tree_oid": "2" * 40,
        "pre_foundation_authority_sha256": "3" * 64,
        "foundation_apply_receipt_sha256": "4" * 64,
        "project_ancestry_evidence_sha256": "5" * 64,
        "project_ancestry_chain_sha256": "6" * 64,
        "resource_ancestor_chain": ["organizations/123456789012"],
        "installed_at_unix": 1_700_000_000,
        "release_path": str(release),
        "release_tree_sha256": tree_sha256,
        "transaction_prefix_sha256": "7" * 64,
        "phase_evidence_sha256": phase_hashes,
        "authority_receipt_public_key_sha256": hashlib.sha256(public_raw).hexdigest(),
        "authority_receipt_public_key_id": public_key_id,
        "credential_id_sha256": bootstrap.EXPECTED_CREDENTIAL_ID_SHA256,
        "executor_hosts_receipt_sha256": "8" * 64,
        "current_release_selected": False,
        "systemd_units_enabled": [],
        "activation_performed": False,
        "activation_seal_created": False,
        "iam_binding_created": False,
        "cloud_mutation_performed": False,
        "caddy_cutover_performed": False,
    }
    signed = {
        **unsigned,
        "receipt_sha256": foundation.sha256_json(unsigned),
    }
    receipt = {
        **signed,
        "signer_key_id": public_key_id,
        "signature_ed25519_b64url": base64
        .urlsafe_b64encode(private_key.sign(foundation.canonical_json_bytes(signed)))
        .rstrip(b"=")
        .decode("ascii"),
    }
    _write(
        layout.state_root / "bootstrap-receipts" / f"install-{REVISION}.json",
        foundation.canonical_json_bytes(receipt),
        mode=0o400,
    )
    return release, receipt


def test_preflight_classifies_exact_inert_restore_without_mutation(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    _release, receipt = _install_trusted_release(layout)
    observer = RecordingObserver(_inert_states(layout))

    result = preflight.preflight_owner_gate_runtime(
        REVISION,
        layout=layout,
        observer=observer,
        expected_uid=UID,
        expected_gid=GID,
    )

    assert result["state"] == "restore_required_inert"
    assert result["release_revision"] == REVISION
    assert result["package_sha256"] == receipt["package_sha256"]
    assert result["current_link_state"] == "absent"
    assert result["mutation_performed"] is False
    assert result["secret_material_recorded"] is False
    assert result["secret_digest_recorded"] is False
    assert observer.calls == [preflight.INSTALLED_UNITS]
    assert not os.path.lexists(layout.current_link)


def test_preflight_classifies_exact_ready_runtime(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    release, _receipt = _install_trusted_release(layout)
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(release)
    observer = RecordingObserver(_ready_states(layout))

    result = preflight.preflight_owner_gate_runtime(
        REVISION,
        layout=layout,
        observer=observer,
        expected_uid=UID,
        expected_gid=GID,
    )

    assert result["state"] == "ready"
    assert result["current_link_state"] == "exact"
    assert result["release_path"] == str(release)
    assert observer.calls == [preflight.INSTALLED_UNITS]


def test_preflight_rejects_unexpected_active_socket_helper(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    release, _receipt = _install_trusted_release(layout)
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(release)
    states = _ready_states(layout)
    states["muncho-passkey-authority.service"] = _unit_values(
        "muncho-passkey-authority.service",
        layout=layout,
        active_state="active",
        sub_state="running",
        unit_file_state="disabled",
    )

    with pytest.raises(
        preflight.OwnerGateRuntimePreflightError,
        match="owner_gate_runtime_unit_state_invalid",
    ):
        preflight.preflight_owner_gate_runtime(
            REVISION,
            layout=layout,
            observer=RecordingObserver(states),
            expected_uid=UID,
            expected_gid=GID,
        )


def test_preflight_rejects_partial_inert_unit_state(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _install_trusted_release(layout)
    states = _inert_states(layout)
    states["muncho-passkey-authority.socket"] = _unit_values(
        "muncho-passkey-authority.socket",
        layout=layout,
        active_state="active",
        sub_state="listening",
        unit_file_state="enabled",
    )

    with pytest.raises(
        preflight.OwnerGateRuntimePreflightError,
        match="owner_gate_runtime_unit_state_invalid",
    ):
        preflight.preflight_owner_gate_runtime(
            REVISION,
            layout=layout,
            observer=RecordingObserver(states),
            expected_uid=UID,
            expected_gid=GID,
        )


def test_preflight_rejects_inert_state_with_activation_seal(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    _install_trusted_release(layout)
    _write(
        layout.etc_root / foundation.MUTATION_ENABLE_SEAL.name,
        b"sealed\n",
        mode=0o440,
    )
    observer = RecordingObserver(_inert_states(layout))

    with pytest.raises(
        preflight.OwnerGateRuntimePreflightError,
        match="owner_gate_runtime_activation_seal_present",
    ):
        preflight.preflight_owner_gate_runtime(
            REVISION,
            layout=layout,
            observer=observer,
            expected_uid=UID,
            expected_gid=GID,
        )

    assert observer.calls == []


def test_preflight_rejects_wrong_current_link(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _install_trusted_release(layout)
    layout.current_link.parent.mkdir(parents=True, exist_ok=True)
    layout.current_link.symlink_to(layout.release_base / ("a" * 40))

    with pytest.raises(
        preflight.OwnerGateRuntimePreflightError,
        match="owner_gate_runtime_current_link_invalid",
    ):
        preflight.preflight_owner_gate_runtime(
            REVISION,
            layout=layout,
            observer=RecordingObserver(_ready_states(layout)),
            expected_uid=UID,
            expected_gid=GID,
        )


def test_preflight_rejects_release_or_installed_unit_drift(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    release, _receipt = _install_trusted_release(layout)
    target = release / "ops/muncho/owner-gate" / "muncho-passkey-authority.socket"
    target.chmod(0o644)

    with pytest.raises(
        preflight.OwnerGateRuntimePreflightError,
        match="owner_gate_runtime_release_evidence_invalid",
    ):
        preflight.preflight_owner_gate_runtime(
            REVISION,
            layout=layout,
            observer=RecordingObserver(_inert_states(layout)),
            expected_uid=UID,
            expected_gid=GID,
        )


def test_preflight_rejects_install_receipt_signature_drift(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    _release, receipt = _install_trusted_release(layout)
    receipt["package_sha256"] = "9" * 64
    receipt_path = layout.state_root / "bootstrap-receipts" / f"install-{REVISION}.json"
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(foundation.canonical_json_bytes(receipt))
    receipt_path.chmod(0o400)

    with pytest.raises(
        preflight.OwnerGateRuntimePreflightError,
        match="owner_gate_runtime_install_receipt_invalid",
    ):
        preflight.preflight_owner_gate_runtime(
            REVISION,
            layout=layout,
            observer=RecordingObserver(_inert_states(layout)),
            expected_uid=UID,
            expected_gid=GID,
        )


def test_systemd_observer_uses_one_fixed_read_only_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    states = _inert_states(layout)
    blocks = [
        "\n".join(f"{key}={value}" for key, value in states[unit].items())
        for unit in preflight.INSTALLED_UNITS
    ]
    calls: list[tuple[str, ...]] = []

    class Completed:
        returncode = 0
        stdout = ("\n\n".join(blocks) + "\n").encode("ascii")
        stderr = b""

    def fake_run(argv: Sequence[str], **kwargs: Any) -> Completed:
        calls.append(tuple(argv))
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is preflight.subprocess.DEVNULL
        return Completed()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)

    observed = preflight.SystemdObserver().observe(preflight.INSTALLED_UNITS)

    assert observed == states
    assert calls == [
        (
            "/usr/bin/systemctl",
            "show",
            "--no-pager",
            f"--property={','.join(preflight.SYSTEMD_PROPERTIES)}",
            *preflight.INSTALLED_UNITS,
        )
    ]
    assert not any(
        command in calls[0]
        for command in ("enable", "disable", "start", "stop", "restart")
    )


def test_owned_file_reader_rejects_reachable_path_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.json"
    _write(path, b'{"original":true}', mode=0o444)
    real_read = preflight.os.read
    replaced = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        raw = real_read(descriptor, size)
        if raw and not replaced:
            replaced = True
            path.unlink()
            _write(path, b'{"replacement":true}', mode=0o444)
        return raw

    monkeypatch.setattr(preflight.os, "read", replacing_read)

    with pytest.raises(
        preflight.OwnerGateRuntimePreflightError,
        match="owner_gate_runtime_test_evidence_invalid",
    ):
        preflight._read_owned_regular(
            path,
            maximum=4096,
            expected_uid=UID,
            expected_gid=GID,
            modes=frozenset({0o444}),
            error_code="owner_gate_runtime_test_evidence_invalid",
        )
