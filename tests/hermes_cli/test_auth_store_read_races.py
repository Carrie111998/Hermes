"""Regression tests for transient Windows auth.json sharing failures."""

from __future__ import annotations

import errno
import hashlib
import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from hermes_cli import auth as auth_mod


def _valid_store_bytes() -> bytes:
    return json.dumps(
        {
            "version": 1,
            "providers": {"test-provider": {"configured": True}},
            "credential_pool": {"test-provider": [{"id": "slot-1"}]},
        }
    ).encode("utf-8")


def _load_corrupt_store_worker(
    auth_path: str,
    start_event,
    result_queue,
) -> None:
    """Load one shared corrupt store from an isolated spawned process."""
    start_event.wait(timeout=10)
    store = auth_mod._load_auth_store(Path(auth_path))
    result_queue.put(store.get(auth_mod._AUTH_STORE_CORRUPT_COPY_KEY))


def test_load_auth_store_retries_transient_windows_read_without_corrupt_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(_valid_store_bytes())
    real_read_bytes = Path.read_bytes
    attempts = 0
    sleeps: list[int] = []

    def locked_then_read(path: Path) -> bytes:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            exc = OSError(errno.EACCES, "sharing violation", str(path))
            exc.winerror = 32
            raise exc
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", locked_then_read)
    monkeypatch.setattr(auth_mod, "_is_windows_sharing_error", lambda _exc: True)
    monkeypatch.setattr(
        auth_mod, "_sleep_before_windows_file_retry", sleeps.append
    )

    store = auth_mod._load_auth_store(auth_file)

    assert attempts == 3
    assert sleeps == [1, 2]
    assert store["providers"]["test-provider"]["configured"] is True
    assert store["credential_pool"]["test-provider"][0]["id"] == "slot-1"
    assert not list(tmp_path.glob("auth.json.corrupt.*"))


def test_load_auth_store_propagates_persistent_windows_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(_valid_store_bytes())
    attempts = 0

    def always_locked(_path: Path) -> bytes:
        nonlocal attempts
        attempts += 1
        exc = OSError(errno.EACCES, "sharing violation", str(auth_file))
        exc.winerror = 32
        raise exc

    monkeypatch.setattr(Path, "read_bytes", always_locked)
    monkeypatch.setattr(auth_mod, "_is_windows_sharing_error", lambda _exc: True)
    monkeypatch.setattr(
        auth_mod, "_sleep_before_windows_file_retry", lambda _attempt: None
    )

    with pytest.raises(OSError) as excinfo:
        auth_mod._load_auth_store(auth_file)

    assert excinfo.value.winerror == 32
    assert attempts == 6
    assert not list(tmp_path.glob("auth.json.corrupt.*"))


def test_load_auth_store_does_not_retry_permanent_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(_valid_store_bytes())
    attempts = 0

    def permanent_failure(_path: Path) -> bytes:
        nonlocal attempts
        attempts += 1
        raise OSError(errno.EIO, "device error", str(auth_file))

    monkeypatch.setattr(Path, "read_bytes", permanent_failure)
    monkeypatch.setattr(auth_mod, "_is_windows_sharing_error", lambda _exc: False)

    with pytest.raises(OSError) as excinfo:
        auth_mod._load_auth_store(auth_file)

    assert excinfo.value.errno == errno.EIO
    assert attempts == 1
    assert not list(tmp_path.glob("auth.json.corrupt.*"))


@pytest.mark.parametrize("payload", [b"{not-json", b'{"providers":{"x":"\xff"}}'])
def test_load_auth_store_preserves_exact_genuinely_invalid_payload(
    tmp_path: Path, payload: bytes
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(payload)

    store = auth_mod._load_auth_store(auth_file)

    assert store["version"] == auth_mod.AUTH_STORE_VERSION
    assert store["providers"] == {}
    assert store[auth_mod._AUTH_STORE_LOAD_FAILED_KEY]
    backups = list(tmp_path.glob("auth.json.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == payload
    assert store[auth_mod._AUTH_STORE_CORRUPT_COPY_KEY] == str(backups[0])
    if os.name == "posix":
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_corrupt_sidecar_is_unique_and_does_not_clobber_existing_copy(
    tmp_path: Path,
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(b"{not-json")
    legacy_copy = tmp_path / "auth.json.corrupt.existing"
    legacy_copy.write_bytes(b"earlier-forensic-copy")

    auth_mod._load_auth_store(auth_file)

    assert legacy_copy.read_bytes() == b"earlier-forensic-copy"
    backups = list(tmp_path.glob("auth.json.corrupt.*"))
    assert len(backups) == 2
    assert any(path.read_bytes() == b"{not-json" for path in backups)


def test_repeated_load_deduplicates_identical_corrupt_payload(tmp_path: Path) -> None:
    auth_file = tmp_path / "auth.json"
    payload = b"{same-invalid-payload"
    auth_file.write_bytes(payload)
    expected = tmp_path / (
        f"auth.json.corrupt.sha256.{hashlib.sha256(payload).hexdigest()}"
    )

    first = auth_mod._load_auth_store(auth_file)
    second = auth_mod._load_auth_store(auth_file)

    assert first[auth_mod._AUTH_STORE_CORRUPT_COPY_KEY] == str(expected)
    assert second[auth_mod._AUTH_STORE_CORRUPT_COPY_KEY] == str(expected)
    assert list(tmp_path.glob("auth.json.corrupt.sha256.*")) == [expected]
    assert expected.read_bytes() == payload


def test_multiprocess_corrupt_load_deduplicates_to_one_sidecar(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only O_EXCL sidecar stress")

    auth_file = tmp_path / "auth.json"
    payload = b"{" + (b"invalid-secret-material" * 32768)
    auth_file.write_bytes(payload)
    expected = tmp_path / (
        f"auth.json.corrupt.sha256.{hashlib.sha256(payload).hexdigest()}"
    )
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_load_corrupt_store_worker,
            args=(str(auth_file), start_event, result_queue),
        )
        for _ in range(4)
    ]
    try:
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=20)

        assert all(not process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0, 0, 0, 0]
        copies = [result_queue.get(timeout=2) for _ in processes]
        assert copies == [str(expected)] * len(processes)
        assert list(tmp_path.glob("auth.json.corrupt.sha256.*")) == [expected]
        assert expected.read_bytes() == payload
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"providers": []},
        {"providers": {}, "credential_pool": []},
        {"credential_pool": "invalid"},
        {"systems": []},
        {"systems": {"nous_portal": []}},
        {"version": 1, "unrecognized": {}},
    ],
)
def test_incompatible_json_structure_uses_same_fail_closed_sentinel(
    tmp_path: Path, raw: object
) -> None:
    auth_file = tmp_path / "auth.json"
    payload = json.dumps(raw).encode("utf-8")
    auth_file.write_bytes(payload)

    store = auth_mod._load_auth_store(auth_file)

    assert store[auth_mod._AUTH_STORE_LOAD_FAILED_KEY]
    sidecar = Path(store[auth_mod._AUTH_STORE_CORRUPT_COPY_KEY])
    assert sidecar.read_bytes() == payload
    with pytest.raises(RuntimeError, match="Refusing to overwrite auth.json"):
        auth_mod._save_auth_store(store, target_path=auth_file)
    assert auth_file.read_bytes() == payload


def test_valid_legacy_systems_store_still_migrates(tmp_path: Path) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "systems": {
                    "nous_portal": {
                        "access_token": "test-access",
                        "refresh_token": "test-refresh",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    store = auth_mod._load_auth_store(auth_file)

    assert auth_mod._AUTH_STORE_LOAD_FAILED_KEY not in store
    assert store["active_provider"] == "nous"
    assert store["providers"]["nous"]["access_token"] == "test-access"


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"version": 1},
        {
            "version": 1,
            "updated_at": "2026-07-25T00:00:00+00:00",
            "active_provider": None,
        },
    ],
)
def test_metadata_only_store_is_valid_and_normalizes_providers(
    tmp_path: Path, raw: dict[str, object]
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps(raw), encoding="utf-8")

    store = auth_mod._load_auth_store(auth_file)

    assert auth_mod._AUTH_STORE_LOAD_FAILED_KEY not in store
    assert store["providers"] == {}
    for key, value in raw.items():
        assert store[key] == value
    assert not list(tmp_path.glob("auth.json.corrupt.sha256.*"))


def test_corrupt_sidecar_has_real_protected_windows_dacl(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only DACL contract")

    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(b"{invalid-with-secret-material")
    store = auth_mod._load_auth_store(auth_file)
    sidecar = Path(store[auth_mod._AUTH_STORE_CORRUPT_COPY_KEY])

    descriptor = win32security.GetFileSecurity(
        str(sidecar),
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
    )
    control, _revision = descriptor.GetSecurityDescriptorControl()
    assert control & win32security.SE_DACL_PROTECTED
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        current_sid = win32security.GetTokenInformation(
            token, win32security.TokenUser
        )[0]
    finally:
        token.Close()
    allowed = {
        win32security.ConvertSidToStringSid(current_sid),
        "S-1-5-18",
    }
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    assert dacl.GetAceCount() == 2
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        assert ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
        assert ace[1] == ntsecuritycon.FILE_ALL_ACCESS
        assert win32security.ConvertSidToStringSid(ace[-1]) in allowed


def test_windows_dacl_failure_never_writes_sidecar_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only DACL contract")

    sidecar = tmp_path / "auth.json.corrupt.sha256.test"
    monkeypatch.setattr(
        auth_mod,
        "_windows_corrupt_sidecar_security_attributes",
        lambda: (_ for _ in ()).throw(PermissionError("DACL unavailable")),
    )

    with pytest.raises(PermissionError, match="DACL unavailable"):
        auth_mod._write_new_corrupt_sidecar_windows(sidecar, b"credential-bytes")

    assert not sidecar.exists()


def test_load_auth_store_real_windows_handle_without_read_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only sharing semantics")

    import ctypes
    from ctypes import wintypes

    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(_valid_store_bytes())
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(auth_file),
        0x80000000,  # GENERIC_READ
        0x00000002 | 0x00000004,  # FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    assert handle != wintypes.HANDLE(-1).value
    released = False

    def release_lock(_attempt: int) -> None:
        nonlocal released
        if released:
            return
        assert close_handle(handle)
        released = True

    monkeypatch.setattr(auth_mod, "_sleep_before_windows_file_retry", release_lock)
    try:
        store = auth_mod._load_auth_store(auth_file)
    finally:
        if not released:
            close_handle(handle)

    assert released, "the first real read must observe the exclusive handle"
    assert store["providers"]["test-provider"]["configured"] is True


def test_save_auth_store_refuses_active_store_loaded_after_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_file = tmp_path / "auth.json"
    monkeypatch.setattr(auth_mod, "_auth_file_path", lambda: auth_file)
    original = b"{not-json"
    auth_file.write_bytes(original)
    store = auth_mod._load_auth_store()
    store.setdefault("credential_pool", {})["test-provider"] = [{"id": "new"}]

    with pytest.raises(RuntimeError, match="Refusing to overwrite auth.json"):
        auth_mod._save_auth_store(store)

    assert auth_file.read_bytes() == original


def test_save_auth_store_refuses_explicit_target_loaded_after_parse_failure(
    tmp_path: Path,
) -> None:
    auth_file = tmp_path / "global-auth.json"
    original = b"{not-json"
    auth_file.write_bytes(original)
    store = auth_mod._load_auth_store(auth_file)
    store.setdefault("credential_pool", {})["test-provider"] = [{"id": "new"}]

    with pytest.raises(RuntimeError, match="Refusing to overwrite auth.json"):
        auth_mod._save_auth_store(store, target_path=auth_file)

    assert auth_file.read_bytes() == original


def test_load_auth_store_missing_file_is_empty_without_corrupt_backup(
    tmp_path: Path,
) -> None:
    auth_file = tmp_path / "auth.json"

    assert auth_mod._load_auth_store(auth_file) == {
        "version": auth_mod.AUTH_STORE_VERSION,
        "providers": {},
    }
    assert not list(tmp_path.glob("auth.json.corrupt.*"))
