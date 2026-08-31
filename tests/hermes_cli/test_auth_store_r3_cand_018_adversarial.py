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
    def fail_link(*args, **kwargs):
        raise OSError("synthetic publication interruption")

    if os.name == "nt":
        monkeypatch.setattr(owner, "_windows_link_sidecar_relative", fail_link)
    else:
        monkeypatch.setattr(owner.os, "link", fail_link)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    assert caught.value.preserved is False
    assert primary.read_bytes() == b"{broken"
    assert not list(tmp_path.glob("*.corrupt"))


def test_sidecar_cleanup_failure_keeps_single_verified_winner(tmp_path, monkeypatch):
    """A post-link temp cleanup fault must not duplicate or lose evidence."""
    primary = tmp_path / "auth.json"
    original = _corrupt(primary, b"{cleanup-race")
    real_unlink = Path.unlink

    def fail_temp_cleanup(path, *args, **kwargs):
        if ".tmp." in path.name:
            raise OSError("synthetic temp cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temp_cleanup)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    assert caught.value.preserved is True
    sidecars = list(tmp_path.glob("auth.json.corrupt*"))
    assert len(sidecars) == 1
    assert sidecars[0].read_bytes() == original


def test_post_publication_verification_failure_stops_without_retry(
    tmp_path, monkeypatch
):
    """A published sidecar remains the single truthful preservation result."""
    primary = tmp_path / "auth.json"
    original = _corrupt(primary, b"{verification-race")
    real_is_reparse_or_link = owner._is_reparse_or_link

    def fail_sidecar_verification(path):
        if path.name.startswith("auth.json.corrupt"):
            raise OSError("synthetic post-publication verification failure")
        return real_is_reparse_or_link(path)

    monkeypatch.setattr(owner, "_is_reparse_or_link", fail_sidecar_verification)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    assert caught.value.preserved is True
    assert caught.value.corrupt_path == primary.with_name("auth.json.corrupt")
    sidecars = list(tmp_path.glob("auth.json.corrupt*"))
    assert len(sidecars) == 1
    assert sidecars[0].read_bytes() == original
    assert not list(tmp_path.glob(".auth.json.corrupt*.tmp.*"))



def test_non_oserror_post_publication_verification_keeps_single_winner(
    tmp_path, monkeypatch
):
    """A non-OSError verification fault cannot orphan or duplicate evidence."""
    primary = tmp_path / "auth.json"
    original = _corrupt(primary, b"{non-oserror-verification")
    real_is_reparse_or_link = owner._is_reparse_or_link
    publication_attempts = 0

    def count_publication(*args, **kwargs):
        nonlocal publication_attempts
        publication_attempts += 1
        return real_publish(*args, **kwargs)

    def fail_sidecar_verification(path):
        if path.name.startswith("auth.json.corrupt"):
            raise RuntimeError("synthetic non-OSError verification failure")
        return real_is_reparse_or_link(path)

    if os.name == "nt":
        real_publish = owner._windows_link_sidecar_relative
        monkeypatch.setattr(owner, "_windows_link_sidecar_relative", count_publication)
    else:
        real_publish = owner.os.link
        monkeypatch.setattr(owner.os, "link", count_publication)
    monkeypatch.setattr(owner, "_is_reparse_or_link", fail_sidecar_verification)

    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    assert caught.value.preserved is True
    assert caught.value.corrupt_path == primary.with_name("auth.json.corrupt")
    sidecars = list(tmp_path.glob("auth.json.corrupt*"))
    assert len(sidecars) == 1
    assert sidecars[0].read_bytes() == original
    assert not list(tmp_path.glob(".auth.json.corrupt*.tmp.*"))
    assert publication_attempts == 1


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


def test_recovery_rejects_caller_digest_for_healthy_store(tmp_path):
    primary = tmp_path / "auth.json"
    auth._save_auth_store({"version": 1, "providers": {"keep": {"ok": True}}}, primary)
    original = primary.read_bytes()

    with pytest.raises(auth.AuthStoreRecoveryConflictError):
        auth.recover_auth_store(
            _replacement(),
            primary,
            expected_corrupt_sha256=hashlib.sha256(original).hexdigest(),
            expected_corrupt_path=primary,
        )

    assert primary.read_bytes() == original


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1},
        {"version": 1, "providers": {}, "metadata": []},
        {"version": 1, "providers": {}, "metadata": "not-an-object"},
    ],
    ids=["missing-section", "metadata-list", "metadata-string"],
)
def test_existing_incomplete_current_schema_is_read_only(tmp_path, payload):
    primary = tmp_path / "auth.json"
    original = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    primary.write_bytes(original)

    with pytest.raises(auth.AuthStoreCorruptionError):
        auth._load_auth_store(primary, allow_legacy_empty=True)

    assert primary.read_bytes() == original


def test_fresh_store_replacement_succeeds_without_snapshot(tmp_path):
    primary = tmp_path / "auth.json"
    auth._save_auth_store({"version": 1, "providers": {"first": {}}}, primary)

    auth._save_auth_store({"version": 1, "providers": {"second": {}}}, primary)

    assert json.loads(primary.read_text(encoding="utf-8"))["providers"] == {"second": {}}


def test_publication_refuses_destination_symlink_without_touching_target(tmp_path, monkeypatch):
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


@pytest.mark.windows_only
def test_windows_sidecar_race_does_not_publish_into_swapped_ancestor(tmp_path, monkeypatch):
    """Sidecar publication must fail closed if its ancestor is swapped."""
    original_parent = tmp_path / "auth-home"
    original_parent.mkdir()
    primary = original_parent / "auth.json"
    primary.write_bytes(b"{broken")
    attacker_parent = tmp_path / "attacker-home"
    attacker_parent.mkdir()
    moved_parent = tmp_path / "auth-home-original"
    real_link = owner._windows_link_sidecar_relative
    swapped = False

    def swap_then_link(source, destination, parent_handle):
        nonlocal swapped
        if not swapped:
            try:
                original_parent.rename(moved_parent)
                original_parent.symlink_to(attacker_parent, target_is_directory=True)
            except OSError as exc:
                # The retained parent handle is expected to deny this rename.
                raise OSError("synthetic ancestor swap blocked") from exc
            swapped = True
            raise OSError("synthetic ancestor swap")
        return real_link(source, destination, parent_handle)

    monkeypatch.setattr(owner, "_windows_link_sidecar_relative", swap_then_link)
    try:
        assert owner._write_corrupt_sidecar(primary, b"credential-bearing-corruption") is None
        assert not list(attacker_parent.glob("*.corrupt*"))
    finally:
        if original_parent.is_symlink():
            original_parent.unlink()
        if moved_parent.exists():
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


def test_recovery_cas_rechecks_after_serialization_callback(tmp_path, monkeypatch):
    """A target mutation after preflight must abort the recovery publication."""
    primary = tmp_path / "auth.json"
    original = _corrupt(primary)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    real_publish = owner._atomic_publish_auth_store

    def race_publish(tmp_path_arg, auth_file, **kwargs):
        assert auth_file == primary
        primary.write_bytes(b"{changed-in-publication-window")
        return real_publish(tmp_path_arg, auth_file, **kwargs)

    monkeypatch.setattr(owner, "_atomic_publish_auth_store", race_publish)
    with pytest.raises(auth.AuthStoreRecoveryConflictError):
        auth.recover_auth_store(
            _replacement(),
            primary,
            expected_corrupt_sha256=hashlib.sha256(original).hexdigest(),
            expected_corrupt_path=primary,
        )
    assert primary.read_bytes() == b"{changed-in-publication-window"


def test_recovery_cas_rechecks_inside_publication_primitive(tmp_path, monkeypatch):
    """Mutation at the final publication boundary must not be overwritten."""
    primary = tmp_path / "auth.json"
    original = _corrupt(primary)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    real_rename = owner._windows_rename_relative

    def mutate_before_rename(source, parent_handle, destination_name, **kwargs):
        primary.write_bytes(b"{external-writer-after-final-check")
        return real_rename(source, parent_handle, destination_name, **kwargs)

    if os.name == "nt":
        monkeypatch.setattr(owner, "_windows_rename_relative", mutate_before_rename)
    else:
        pytest.skip("native publication-boundary probe is Windows-specific")

    with pytest.raises(auth.AuthStoreRecoveryConflictError):
        auth.recover_auth_store(
            _replacement(),
            primary,
            expected_corrupt_sha256=hashlib.sha256(original).hexdigest(),
            expected_corrupt_path=primary,
        )
    assert primary.read_bytes() == b"{external-writer-after-final-check"




def test_posix_publication_remains_bound_after_ancestor_swap(tmp_path, monkeypatch):
    """A retained parent fd must publish into the checked directory."""
    if os.name == "nt":
        pytest.skip("POSIX dirfd publication regression")
    original_parent = tmp_path / "auth-home"
    original_parent.mkdir()
    primary = original_parent / "auth.json"
    auth._save_auth_store(
        {"version": auth.AUTH_STORE_VERSION, "providers": {"original": {}}},
        primary,
    )
    store = auth._load_auth_store(primary)
    store["providers"]["original"]["changed"] = True
    attacker_parent = tmp_path / "attacker-home"
    attacker_parent.mkdir()
    (attacker_parent / "auth.json").write_bytes(b"attacker-owned")
    moved_parent = tmp_path / "auth-home-original"
    real_replace = owner.os.replace
    swapped = False

    def swap_before_publish(source, destination, **kwargs):
        nonlocal swapped
        if destination == "auth.json" and not swapped:
            original_parent.rename(moved_parent)
            original_parent.symlink_to(attacker_parent, target_is_directory=True)
            swapped = True
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(owner.os, "replace", swap_before_publish)
    try:
        auth._save_auth_store(store, primary)
        assert swapped is True
        assert json.loads((moved_parent / "auth.json").read_text(encoding="utf-8"))["providers"] == {
            "original": {"changed": True}
        }
        assert (attacker_parent / "auth.json").read_bytes() == b"attacker-owned"
    finally:
        if original_parent.is_symlink():
            original_parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(original_parent)


@pytest.mark.windows_only
def test_windows_publication_rejects_ancestor_swap_before_source_open(tmp_path, monkeypatch):
    """A retained parent handle must reject a deterministic ancestor swap."""
    original_parent = tmp_path / "auth-home"
    original_parent.mkdir()
    source = original_parent / "source"
    source.write_bytes(b"source")
    attacker_parent = tmp_path / "attacker-home"
    attacker_parent.mkdir()
    (attacker_parent / "source").write_bytes(b"attacker")
    moved_parent = tmp_path / "auth-home-original"
    parent_handle = owner._windows_open_no_reparse(
        original_parent,
        directory=True,
        share_mode=7,
    )
    real_final_path = owner._windows_final_path
    calls = 0

    def swap_after_parent_validation(path, handle, win32file):
        nonlocal calls
        result = real_final_path(path, handle, win32file)
        calls += 1
        if calls == 1:
            original_parent.rename(moved_parent)
            original_parent.symlink_to(attacker_parent, target_is_directory=True)
        return result

    monkeypatch.setattr(owner, "_windows_final_path", swap_after_parent_validation)
    try:
        with pytest.raises(OSError, match="escaped"):
            owner._windows_rename_relative(
                source,
                parent_handle,
                "published",
                replace_existing=True,
            )
        assert not (attacker_parent / "published").exists()
    finally:
        import win32file

        win32file.CloseHandle(parent_handle)
        if original_parent.is_symlink():
            original_parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(original_parent)


def test_windows_lock_init_permission_error_retries_until_timeout(tmp_path, monkeypatch):
    """A raced lock-file init must reach the bounded acquisition timeout."""
    lock_path = tmp_path / "auth.lock"
    init_attempts = []
    lock_attempts = []

    class RacingMsvcrt:
        LK_NBLCK = object()

        @staticmethod
        def locking(*args, **kwargs):
            lock_attempts.append(args)
            raise PermissionError("synthetic concurrent lock-file race")

    real_write_text = Path.write_text

    def fail_lock_initialization(path, *args, **kwargs):
        if path == lock_path:
            init_attempts.append(path)
            raise PermissionError("synthetic concurrent lock-file init race")
        return real_write_text(path, *args, **kwargs)

    monkeypatch.setattr(owner, "msvcrt", RacingMsvcrt())
    monkeypatch.setattr(Path, "write_text", fail_lock_initialization)
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(owner.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(owner.time, "sleep", lambda _seconds: None)

    holder = type("Holder", (), {})()
    with pytest.raises(TimeoutError, match="synthetic lock timeout"):
        with owner._file_lock(lock_path, holder, 0.0, "synthetic lock timeout"):
            pass

    assert init_attempts == [lock_path]
    assert lock_attempts == []
