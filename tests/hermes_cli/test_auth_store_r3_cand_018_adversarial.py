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


def test_auth_recover_command_imports_valid_json(tmp_path, capsys):
    primary = tmp_path / "auth.json"
    _corrupt(primary)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(_replacement("cli")), encoding="utf-8")

    from hermes_cli.auth_commands import auth_recover_command

    auth_recover_command(type("Args", (), {"target": str(primary), "source": str(replacement)})())
    assert json.loads(primary.read_text(encoding="utf-8"))["providers"] == {"cli": {"ok": True}}
    assert "Recovered auth store:" in capsys.readouterr().out
