"""Adversarial filesystem and transaction contracts for auth-store recovery."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import hermes_cli.auth as auth
import hermes_cli.auth_store as owner


def _corrupt(path: Path, payload: bytes = b"{broken") -> bytes:
    path.write_bytes(payload)
    return payload


def _replacement(provider: str = "recovered") -> dict:
    return {"version": auth.AUTH_STORE_VERSION, "providers": {provider: {"ok": True}}}


def test_collision_never_overwrites_incumbent_sidecar(tmp_path):
    primary = tmp_path / "auth.json"
    original = _corrupt(primary, b"{first-corrupt")
    incumbent = primary.with_name("auth.json.corrupt")
    incumbent.write_bytes(b"first-evidence")

    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    assert caught.value.preserved is True
    assert caught.value.corrupt_path != incumbent
    assert incumbent.read_bytes() == b"first-evidence"
    assert caught.value.corrupt_path.read_bytes() == original


def test_sidecar_symlink_is_not_followed(tmp_path):
    primary = tmp_path / "auth.json"
    target = tmp_path / "selected-by-attacker"
    target.write_bytes(b"sentinel")
    incumbent = primary.with_name("auth.json.corrupt")
    try:
        incumbent.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary) if primary.exists() else (_corrupt(primary), auth._load_auth_store(primary))[1]

    assert target.read_bytes() == b"sentinel"
    assert caught.value.preserved is True
    assert caught.value.corrupt_path != incumbent


def test_interrupted_sidecar_publication_leaves_no_partial_destination(tmp_path, monkeypatch):
    primary = tmp_path / "auth.json"
    _corrupt(primary)
    real_link = os.link

    def fail_link(*args, **kwargs):
        raise OSError("synthetic publication interruption")

    monkeypatch.setattr(owner.os, "link", fail_link)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    assert caught.value.preserved is False
    assert primary.read_bytes() == b"{broken"
    assert not list(tmp_path.glob("*.corrupt"))
    # Restore explicitly so this test remains clear if the implementation
    # imports os through another alias in a future extraction.
    monkeypatch.setattr(owner.os, "link", real_link)


def test_recovery_rejects_changed_reviewed_source(tmp_path):
    primary = tmp_path / "auth.json"
    original = _corrupt(primary)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)
    digest = caught.value.corrupt_sha256
    assert digest == hashlib.sha256(original).hexdigest()

    primary.write_bytes(b"{changed-before-recovery")
    with pytest.raises(auth.AuthStoreRecoveryConflictError):
        auth.recover_auth_store(
            _replacement(),
            primary,
            expected_corrupt_sha256=digest,
            expected_corrupt_path=primary,
        )
    assert primary.read_bytes() == b"{changed-before-recovery"


def test_stale_loaded_writer_cannot_erase_recovery(tmp_path):
    primary = tmp_path / "auth.json"
    auth._save_auth_store({"version": 1, "providers": {"old": {"v": 1}}}, primary)
    stale = auth._load_auth_store(primary)
    _corrupt(primary)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    auth.recover_auth_store(
        _replacement(),
        primary,
        expected_corrupt_sha256=caught.value.corrupt_sha256,
        expected_corrupt_path=primary,
    )
    with pytest.raises(auth.AuthStoreWriteConflictError):
        auth._save_auth_store(stale, primary)
    assert json.loads(primary.read_text(encoding="utf-8"))["providers"] == {
        "recovered": {"ok": True}
    }


def test_recovery_schema_rejection_is_read_only(tmp_path):
    primary = tmp_path / "auth.json"
    original = _corrupt(primary)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    with pytest.raises(ValueError):
        auth.recover_auth_store(
            {"version": 1, "providers": ["not-an-object"]},
            primary,
            expected_corrupt_sha256=caught.value.corrupt_sha256,
            expected_corrupt_path=primary,
        )
    assert primary.read_bytes() == original


@pytest.mark.parametrize(
    "payload",
    [
        17,
        [],
        {"version": 1, "providers": []},
        {"version": 1, "providers": {"bad": []}},
        {"version": 1, "providers": {}, "credential_pool": []},
        {"version": 1, "providers": {}, "credential_pool": {"x": {}}},
        {"version": 1, "providers": {}, "credential_pool": {"x": ["bad"]}},
    ],
    ids=["scalar", "list", "providers-list", "provider-entry", "pool-list", "pool-entry", "pool-item"],
)
def test_valid_json_invalid_shape_is_read_only_and_preserved(tmp_path, payload):
    primary = tmp_path / "auth.json"
    original = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    primary.write_bytes(original)

    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    assert caught.value.corrupt_sha256 == hashlib.sha256(original).hexdigest()
    assert caught.value.preserved is True
    assert primary.read_bytes() == original
    assert caught.value.corrupt_path.read_bytes() == original


@pytest.mark.parametrize(
    "replacement",
    [
        {},
        {"providers": {}},
        {"version": 1},
        {"version": 1, "providers": []},
        {"version": 1, "providers": {}, "credential_pool": {"x": [None]}},
    ],
    ids=["empty", "missing-version", "missing-providers", "providers-list", "pool-item"],
)
def test_recovery_requires_complete_canonical_schema(tmp_path, replacement):
    primary = tmp_path / "auth.json"
    original = _corrupt(primary)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    with pytest.raises(ValueError):
        auth.recover_auth_store(
            replacement,
            primary,
            expected_corrupt_sha256=caught.value.corrupt_sha256,
            expected_corrupt_path=primary,
        )
    assert primary.read_bytes() == original


def test_recovery_requires_reviewed_digest(tmp_path):
    primary = tmp_path / "auth.json"
    _corrupt(primary)
    with pytest.raises(auth.AuthStoreCorruptionError):
        auth._load_auth_store(primary)

    with pytest.raises(ValueError, match="reviewed corrupt-store SHA-256"):
        auth.recover_auth_store(_replacement(), primary)
    assert primary.read_bytes() == b"{broken"


def test_publication_refuses_destination_symlink_without_touching_target(tmp_path):
    primary = tmp_path / "auth.json"
    attacker_target = tmp_path / "attacker-target"
    attacker_bytes = b"attacker-owned"
    attacker_target.write_bytes(attacker_bytes)
    try:
        primary.symlink_to(attacker_target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(OSError):
        auth._save_auth_store(_replacement(), primary)

    assert primary.is_symlink()
    assert attacker_target.read_bytes() == attacker_bytes


@pytest.mark.windows_only
def test_windows_final_component_swap_between_check_and_open_is_rejected(tmp_path, monkeypatch):
    primary = tmp_path / "auth.json"
    primary.write_bytes(b'{"version":1,"providers":{}}')
    attacker_target = tmp_path / "attacker-target"
    attacker_target.write_bytes(b'{"providers":{"attacker":{}}}')
    swapped = False

    def swap_after_lexical_check(path):
        nonlocal swapped
        if not swapped:
            path.unlink()
            path.symlink_to(attacker_target)
            swapped = True
        return False

    monkeypatch.setattr(owner, "_is_reparse_or_link", swap_after_lexical_check)
    with pytest.raises(OSError):
        owner._read_auth_bytes(primary)
    assert swapped is True
    assert attacker_target.read_bytes() == b'{"providers":{"attacker":{}}}'


@pytest.mark.windows_only
def test_windows_ancestor_swap_between_check_and_open_is_rejected(tmp_path):
    original_parent = tmp_path / "auth-home"
    original_parent.mkdir()
    primary = original_parent / "auth.json"
    primary.write_bytes(b'{"version":1,"providers":{}}')
    attacker_parent = tmp_path / "attacker-home"
    attacker_parent.mkdir()
    (attacker_parent / "auth.json").write_bytes(b'{"providers":{"attacker":{}}}')
    moved_parent = tmp_path / "auth-home-original"
    try:
        original_parent.rename(moved_parent)
        original_parent.symlink_to(attacker_parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if original_parent.is_symlink():
            original_parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(original_parent)
        pytest.skip(f"directory symlinks unavailable: {exc}")

    try:
        with pytest.raises(OSError):
            owner._read_auth_bytes(primary)
        assert (attacker_parent / "auth.json").read_bytes() == b'{"providers":{"attacker":{}}}'
    finally:
        original_parent.unlink()
        moved_parent.rename(original_parent)


def test_auth_recover_command_imports_valid_json(tmp_path, capsys):
    primary = tmp_path / "auth.json"
    _corrupt(primary)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(_replacement("cli")), encoding="utf-8")

    from hermes_cli.auth_commands import auth_recover_command

    auth_recover_command(type("Args", (), {"target": str(primary), "source": str(replacement)})())
    assert json.loads(primary.read_text(encoding="utf-8"))["providers"] == {"cli": {"ok": True}}
    assert "Recovered auth store:" in capsys.readouterr().out
