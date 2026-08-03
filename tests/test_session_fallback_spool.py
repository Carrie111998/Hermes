import hashlib
import json
import errno
import os
import stat
import threading
from pathlib import Path

import pytest

from hermes_state import SessionDBBatchMessage
import session_fallback_spool as spool

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows-only fallback
    fcntl = None


@pytest.fixture()
def spool_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _bootstrap() -> spool.SessionSpoolBootstrap:
    return spool.SessionSpoolBootstrap(
        session_id="session-123",
        source="cli",
        started_at=123.456,
        model="gpt-test",
        model_config={"max_iterations": 2},
        system_prompt="system prompt",
        parent_session_id=None,
        cwd="/tmp/project",
        profile_name=None,
        user_id="user-1",
        session_key="session-key",
        chat_id="chat-1",
        chat_type="group",
        thread_id="thread-1",
    )


def _batch_messages(
    unit_id: str = "unit-1",
    *,
    contents: tuple[str, ...] = ("hello",),
    timestamp: float = 100.0,
) -> tuple[SessionDBBatchMessage, ...]:
    return tuple(
        SessionDBBatchMessage(
            persistence_unit_id=unit_id,
            persistence_message_key=f"{unit_id}-key-{idx}",
            persistence_ordinal=idx,
            role="assistant" if idx else "user",
            content=content,
            timestamp=timestamp + idx,
        )
        for idx, content in enumerate(contents)
    )


def _record(
    unit_id: str = "unit-1",
    *,
    attempt_index: int = 0,
    contents: tuple[str, ...] = ("hello",),
) -> spool.SessionSpoolRecord:
    return spool.SessionSpoolRecord(
        bootstrap=_bootstrap(),
        persist_attempt_id="a" * 32,
        persist_attempt_unit_index=attempt_index,
        canonical_failure={
            "stage": "append_messages_batch",
            "error_class": "RuntimeError",
            "error_message": "db down",
            "session_row_created": True,
        },
        batch_messages=_batch_messages(unit_id, contents=contents),
    )


def _paths(home: Path) -> tuple[Path, Path, Path, Path]:
    root = home / "session_fallback_spool"
    return root, root / "active.spool", root / "append.lock", root / "quarantine"


def _fd_count() -> int:
    import psutil

    return psutil.Process().num_fds()


def test_frame_bytes_are_deterministic_and_schema_validation_is_strict(spool_home):
    record = _record()
    frame_one = spool._frame_bytes_for_record(record)
    frame_two = spool._frame_bytes_for_record(record)

    assert frame_one == frame_two
    assert frame_one[:4] == b"HSPL"
    assert int.from_bytes(frame_one[8:16], "big") == len(frame_one) - 32
    assert frame_one[16:32].hex() == hashlib.blake2s(
        frame_one[4:16] + frame_one[32:], digest_size=16
    ).hexdigest()

    _, active_path, _, _ = _paths(spool_home)
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_bytes(spool._frame_from_payload_bytes(b'{"schema_version":1}'))

    scan = spool.scan_spool(active_path)
    assert scan.tail_status is spool.SpoolTailStatus.INVALID_SCHEMA
    assert scan.valid_prefix_bytes == 0
    assert scan.frame_count == 0


def test_append_creates_private_profile_local_layout_and_modes(spool_home):
    result = spool.append_records((_record(),))
    receipt = result.unit_results[0].receipt
    root, active_path, lock_path, quarantine_path = _paths(spool_home)

    assert root == spool_home / "session_fallback_spool"
    assert receipt.path == str(active_path)
    assert active_path.exists()
    assert lock_path.exists()
    assert quarantine_path.exists()
    assert str(root).startswith(str(spool_home))

    if os.name == "posix":
        assert (root.stat().st_mode & 0o777) == 0o700
        assert (quarantine_path.stat().st_mode & 0o777) == 0o700
        assert (active_path.stat().st_mode & 0o777) == 0o600
        assert (lock_path.stat().st_mode & 0o777) == 0o600

    scan = spool.scan_spool(active_path)
    assert scan.tail_status is spool.SpoolTailStatus.CLEAN
    assert scan.frame_count == 1
    assert receipt.offset == 0


@pytest.mark.skipif(os.name != "posix", reason="symlink security is POSIX-only")
def test_symlinked_root_is_refused(spool_home):
    root, _, _, _ = _paths(spool_home)
    target = spool_home / "other-root"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(spool.SpoolPathSecurityError):
        spool.append_records((_record(),))

    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="symlink race schedule is POSIX-only")
def test_root_swap_after_preflight_is_rejected(spool_home, monkeypatch):
    external = spool_home / "external-root"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")
    real_assert = spool._assert_entry_matches_fd
    swapped = {"done": False}

    def _swap(parent_fd, name, fd, *, expect, label):
        if (
            not swapped["done"]
            and name == spool.SPOOL_ROOT_NAME
            and label == str(spool._spool_root())
        ):
            swapped["done"] = True
            root = spool._spool_root()
            parked = root.with_name(root.name + ".real")
            os.replace(root, parked)
            os.mkdir(root, mode=0o755)
        return real_assert(parent_fd, name, fd, expect=expect, label=label)

    monkeypatch.setattr(spool, "_assert_entry_matches_fd", _swap)

    with pytest.raises(spool.SessionFallbackSpoolError):
        spool.append_records((_record(),))

    replacement_root = spool._spool_root()
    assert replacement_root.is_dir()
    if os.name == "posix":
        assert (replacement_root.stat().st_mode & 0o777) == 0o755
    assert not (replacement_root / "quarantine").exists()
    assert not (replacement_root / "active.spool").exists()
    assert sentinel.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(os.name != "posix", reason="quarantine race schedule is POSIX-only")
def test_quarantine_dir_swap_after_preflight_is_rejected(spool_home, monkeypatch):
    root, active_path, _, quarantine_path = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean_frame = spool._frame_bytes_for_record(_record("unit-clean"))
    active_path.write_bytes(clean_frame[:-1])
    external = spool_home / "external-quarantine"
    external.mkdir()

    real_next = spool._next_quarantine_sequence
    swapped = {"done": False}

    def _swap(path: Path) -> int:
        seq = real_next(path)
        if not swapped["done"]:
            swapped["done"] = True
            parked = path.with_name(path.name + ".real")
            os.replace(path, parked)
            os.symlink(external, path, target_is_directory=True)
        return seq

    monkeypatch.setattr(spool, "_next_quarantine_sequence", _swap)

    with pytest.raises(spool.SessionFallbackSpoolError):
        spool.append_records((_record("unit-fresh"),))

    assert list(external.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="active rename race schedule is POSIX-only")
def test_active_name_swap_before_receipt_is_rejected(spool_home, monkeypatch):
    root, active_path, _, quarantine_path = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    active_path.write_bytes(b"")
    external_target = spool_home / "external-active"
    external_target.write_bytes(b"")

    real_fsync = spool._fsync_fd
    seen = {"count": 0}

    def _swap(fd: int) -> None:
        seen["count"] += 1
        if seen["count"] == 1:
            parked = active_path.with_name(active_path.name + ".real")
            os.replace(active_path, parked)
            os.symlink(external_target, active_path)
        real_fsync(fd)

    monkeypatch.setattr(spool, "_fsync_fd", _swap)

    with pytest.raises(spool.SessionFallbackSpoolError):
        spool.append_records((_record("unit-live"),))

    assert external_target.read_bytes() == b""


@pytest.mark.skipif(os.name != "posix", reason="sidecar race schedule is POSIX-only")
def test_sidecar_destination_swap_is_rejected(spool_home, monkeypatch):
    root, active_path, _, quarantine_path = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean_frame = spool._frame_bytes_for_record(_record("unit-clean"))
    active_path.write_bytes(clean_frame[:-1])
    external_target = spool_home / "external-sidecar"
    external_target.write_text("outside", encoding="utf-8")

    real_fsync = spool._fsync_fd
    final_sidecar = quarantine_path / "000001-incomplete_eof-vp0.json"
    injected = {"done": False}

    def _swap(fd: int) -> None:
        if not injected["done"]:
            temp_files = list(quarantine_path.glob("*.tmp"))
            if temp_files:
                injected["done"] = True
                os.symlink(external_target, final_sidecar)
        real_fsync(fd)

    monkeypatch.setattr(spool, "_fsync_fd", _swap)

    with pytest.raises(spool.SessionFallbackSpoolError):
        spool.append_records((_record("unit-fresh"),))

    assert external_target.read_text(encoding="utf-8") == "outside"


def test_unknown_record_kind_is_rejected_by_scan(spool_home):
    payload = spool._payload_bytes_for_record(_record())
    _, active_path, _, _ = _paths(spool_home)
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_bytes(spool._frame_from_payload_bytes(payload, record_kind=0x02))

    scan = spool.scan_spool(active_path)
    assert scan.tail_status is spool.SpoolTailStatus.BAD_RECORD_KIND


def test_nonzero_reserved_header_bytes_are_rejected_by_scan(spool_home):
    payload = spool._payload_bytes_for_record(_record())
    payload_len = len(payload)
    header_prefix = bytes(
        [spool.FRAME_VERSION, spool.RECORD_KIND_SESSION_PERSISTENCE_UNIT, 0x12, 0x34]
    ) + payload_len.to_bytes(8, "big")
    digest = hashlib.blake2s(header_prefix + payload, digest_size=16).digest()
    frame = spool.HEADER_MAGIC + header_prefix + digest + payload
    _, active_path, _, _ = _paths(spool_home)
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_bytes(frame)

    scan = spool.scan_spool(active_path)
    assert scan.tail_status is spool.SpoolTailStatus.NONZERO_RESERVED


@pytest.mark.skipif(fcntl is None, reason="POSIX flock required")
def test_non_contention_lock_errors_fail_immediately(spool_home, monkeypatch):
    monkeypatch.setattr(spool, "LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(spool, "LOCK_RETRY_SECONDS", 0.01)

    def _boom(_fd: int, _op: int) -> None:
        raise OSError(errno.EBADF, "bad fd")

    monkeypatch.setattr(spool.fcntl, "flock", _boom)

    with pytest.raises(spool.SpoolDurabilityError):
        spool.append_records((_record(),))


def test_invalid_ordinal_record_is_rejected_before_receipt(spool_home):
    invalid = spool.SessionSpoolRecord(
        bootstrap=_bootstrap(),
        persist_attempt_id="b" * 32,
        persist_attempt_unit_index=0,
        canonical_failure={
            "stage": "append_messages_batch",
            "error_class": "RuntimeError",
            "error_message": "db down",
            "session_row_created": True,
        },
        batch_messages=(
            SessionDBBatchMessage(
                persistence_unit_id="unit-invalid",
                persistence_message_key="key-invalid",
                persistence_ordinal=7,
                role="user",
                content="bad order",
                timestamp=1.0,
            ),
        ),
    )

    with pytest.raises(spool.SessionFallbackSpoolError):
        spool.append_records((invalid,))

    _, active_path, _, _ = _paths(spool_home)
    assert not active_path.exists() or active_path.stat().st_size == 0


def test_scan_budget_exceeded_is_not_reported_clean(spool_home):
    frame_one = spool._frame_bytes_for_record(_record("unit-a"))
    frame_two = spool._frame_bytes_for_record(_record("unit-b"))
    _, active_path, _, _ = _paths(spool_home)
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_bytes(frame_one + frame_two)

    scan = spool.scan_spool(active_path, max_file_bytes=len(frame_one))

    assert scan.tail_status is not spool.SpoolTailStatus.CLEAN
    assert scan.valid_prefix_bytes == len(frame_one)
    assert scan.tail_offset == len(frame_one)


def test_missing_sidecar_is_reconciled_before_new_receipt(spool_home, monkeypatch):
    root, active_path, _, quarantine_path = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean_frame = spool._frame_bytes_for_record(_record("unit-clean"))
    active_path.write_bytes(clean_frame[:-1])

    def _boom(*_args, **_kwargs):
        raise OSError("sidecar write failed")

    real_write_sidecar_json = spool._write_sidecar_json
    monkeypatch.setattr(spool, "_write_sidecar_json", _boom)
    with pytest.raises(OSError):
        spool.append_records((_record("unit-first"),))

    monkeypatch.setattr(spool, "_write_sidecar_json", real_write_sidecar_json)
    result = spool.append_records((_record("unit-second"),))

    assert len(result.unit_results) == 1
    assert sorted(quarantine_path.glob("*.spool"))
    assert sorted(quarantine_path.glob("*.json"))


def test_parent_fsync_failure_on_created_active_must_be_reestablished_before_receipt(
    spool_home, monkeypatch
):
    root, active_path, lock_path, quarantine_path = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path.write_bytes(b"")
    if os.name == "posix":
        os.chmod(root, 0o700)
        os.chmod(quarantine_path, 0o700)
        os.chmod(lock_path, 0o600)

    real_dir_fsync = spool._fsync_directory_fd
    fail_parent = {"enabled": True}
    root_fsync_calls = {"count": 0}

    def _fail_root(fd: int, label):
        if str(label) == str(root):
            root_fsync_calls["count"] += 1
        if (
            fail_parent["enabled"]
            and str(label) == str(root)
            and root_fsync_calls["count"] >= 3
        ):
            raise OSError("simulated parent fsync failure")
        return real_dir_fsync(fd, label)

    monkeypatch.setattr(spool, "_fsync_directory_fd", _fail_root)

    with pytest.raises(spool.SpoolDurabilityError):
        spool.append_records((_record("unit-first"),))

    assert active_path.exists()
    assert active_path.stat().st_size == 0

    with pytest.raises(spool.SpoolDurabilityError):
        spool.append_records((_record("unit-second"),))

    fail_parent["enabled"] = False
    result = spool.append_records((_record("unit-third"),))
    assert len(result.unit_results) == 1
    assert result.unit_results[0].receipt.offset == 0


@pytest.mark.skipif(os.name != "posix", reason="fd-count regression is POSIX-only")
def test_repeated_parent_fsync_failures_do_not_leak_fds_on_lock_open(spool_home, monkeypatch):
    root, _, lock_path, quarantine_path = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path.write_bytes(b"")
    if os.name == "posix":
        os.chmod(root, 0o700)
        os.chmod(quarantine_path, 0o700)
        os.chmod(lock_path, 0o600)

    baseline = _fd_count()
    root_stat = root.stat()
    real_fsync = spool.os.fsync

    def _boom(fd: int):
        fd_stat = os.fstat(fd)
        if stat.S_ISDIR(fd_stat.st_mode) and (
            fd_stat.st_dev == root_stat.st_dev and fd_stat.st_ino == root_stat.st_ino
        ):
            raise OSError("simulated root-directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(spool.os, "fsync", _boom)

    for attempt in range(25):
        with pytest.raises(spool.SpoolDurabilityError):
            spool.append_records((_record(f"unit-lock-{attempt}"),))
        assert _fd_count() == baseline


@pytest.mark.skipif(os.name != "posix", reason="fd-count regression is POSIX-only")
def test_repeated_parent_fsync_failures_do_not_leak_fds_on_quarantine_open(
    spool_home, monkeypatch
):
    root, active_path, lock_path, quarantine_path = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path.write_bytes(b"")
    active_path.write_bytes(b"")
    if os.name == "posix":
        os.chmod(root, 0o700)
        os.chmod(quarantine_path, 0o700)
        os.chmod(lock_path, 0o600)
        os.chmod(active_path, 0o600)

    baseline = _fd_count()
    root_stat = root.stat()
    real_fsync = spool.os.fsync

    for attempt in range(25):
        root_fsync_calls = {"count": 0}

        def _boom(fd: int):
            fd_stat = os.fstat(fd)
            if stat.S_ISDIR(fd_stat.st_mode) and (
                fd_stat.st_dev == root_stat.st_dev and fd_stat.st_ino == root_stat.st_ino
            ):
                root_fsync_calls["count"] += 1
                if root_fsync_calls["count"] >= 2:
                    raise OSError("simulated quarantine-parent fsync failure")
            return real_fsync(fd)

        monkeypatch.setattr(spool.os, "fsync", _boom)
        with pytest.raises(spool.SpoolDurabilityError):
            spool.append_records((_record(f"unit-dir-{attempt}"),))
        assert _fd_count() == baseline


def test_file_fsync_failure_produces_no_receipt(spool_home, monkeypatch):
    real_fsync = spool._fsync_fd
    seen = {"file": 0}

    def _boom(fd: int) -> None:
        seen["file"] += 1
        if seen["file"] >= 2:
            raise OSError("fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(spool, "_fsync_fd", _boom)

    with pytest.raises(spool.SpoolDurabilityError):
        spool.append_records((_record(),))

    _, active_path, _, _ = _paths(spool_home)
    assert active_path.exists()
    assert active_path.stat().st_size > 0


def test_directory_fsync_failure_on_create_produces_no_receipt(spool_home, monkeypatch):
    def _boom(_fd: int, _label) -> None:
        raise OSError("dir fsync failed")

    monkeypatch.setattr(spool, "_fsync_directory_fd", _boom)

    with pytest.raises(spool.SpoolDurabilityError):
        spool.append_records((_record(),))

    root, _, _, _ = _paths(spool_home)
    assert root.exists()


@pytest.mark.skipif(fcntl is None, reason="POSIX flock required")
def test_lock_timeout_is_bounded(spool_home, monkeypatch):
    root, _, lock_path, _ = _paths(spool_home)
    root.mkdir(mode=0o700)
    lock_path.touch(mode=0o600)

    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(spool, "LOCK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(spool, "LOCK_RETRY_SECONDS", 0.01)
        with pytest.raises(spool.SpoolLockTimeoutError):
            spool.append_records((_record(),))


@pytest.mark.skipif(fcntl is None, reason="POSIX flock required")
def test_concurrent_writers_serialize_and_do_not_overlap_offsets(spool_home):
    records = [_record("unit-a"), _record("unit-b")]
    results = []
    errors = []

    def _worker(item):
        try:
            results.append(spool.append_records((item,)).unit_results[0].receipt)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(item,)) for item in records]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(results) == 2
    ordered = sorted(results, key=lambda receipt: receipt.offset)
    assert ordered[0].offset == 0
    assert ordered[1].offset == ordered[0].frame_length

    _, active_path, _, _ = _paths(spool_home)
    scan = spool.scan_spool(active_path)
    assert scan.tail_status is spool.SpoolTailStatus.CLEAN
    assert scan.frame_count == 2


def test_capacity_includes_quarantine_bytes_and_refuses_before_append(spool_home, monkeypatch):
    first = spool.append_records((_record("unit-a"),)).unit_results[0].receipt
    root, active_path, _, quarantine_path = _paths(spool_home)
    quarantine_spool = quarantine_path / "000001-clean-vp0.spool"
    quarantine_bytes = spool._frame_bytes_for_record(_record("unit-q"))
    quarantine_spool.write_bytes(quarantine_bytes)
    (quarantine_path / "000001-clean-vp0.json").write_text(
        json.dumps(
            {
                "sequence": 1,
                "tail_status": "clean",
                "valid_prefix_bytes": len(quarantine_bytes),
                "original_size": len(quarantine_bytes),
                "quarantined_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    if os.name == "posix":
        os.chmod(quarantine_spool, 0o600)

    second_frame = spool._frame_bytes_for_record(_record("unit-b"))
    monkeypatch.setattr(
        spool,
        "TOTAL_CAP_BYTES",
        active_path.stat().st_size + quarantine_spool.stat().st_size + len(second_frame) - 1,
    )
    before = active_path.read_bytes()

    with pytest.raises(spool.SpoolCapacityError):
        spool.append_records((_record("unit-b"),))

    assert active_path.read_bytes() == before
    assert first.frame_length == len(before)


_CORRUPT_TAIL_CASES = (
    (
        spool.SpoolTailStatus.INCOMPLETE_EOF,
        lambda frame: frame[:-1],
    ),
    (
        spool.SpoolTailStatus.BAD_MAGIC,
        lambda frame: b"NOPE" + frame[4:],
    ),
    (
        spool.SpoolTailStatus.BAD_VERSION,
        lambda frame: frame[:4] + b"\x02" + frame[5:],
    ),
    (
        spool.SpoolTailStatus.OVERSIZED_LENGTH,
        lambda frame: frame[:8]
        + (spool.MAX_PAYLOAD_BYTES + 1).to_bytes(8, "big")
        + frame[16:],
    ),
    (
        spool.SpoolTailStatus.CHECKSUM_MISMATCH,
        lambda frame: frame[:20] + bytes([frame[20] ^ 0xFF]) + frame[21:],
    ),
    (
        spool.SpoolTailStatus.INVALID_JSON,
        lambda _frame: spool._frame_from_payload_bytes(b"{"),
    ),
    (
        spool.SpoolTailStatus.INVALID_SCHEMA,
        lambda _frame: spool._frame_from_payload_bytes(b'{"schema_version":1}'),
    ),
)


@pytest.mark.parametrize(("status", "corrupt"), _CORRUPT_TAIL_CASES)
def test_corrupt_tails_are_quarantined_with_valid_prefix(
    spool_home,
    status,
    corrupt,
):
    root, active_path, _, quarantine_path = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(root, 0o700)
        os.chmod(quarantine_path, 0o700)

    clean_frame = spool._frame_bytes_for_record(_record("unit-clean"))
    corrupt_frame = corrupt(spool._frame_bytes_for_record(_record("unit-bad")))
    original_bytes = clean_frame + corrupt_frame
    active_path.write_bytes(original_bytes)
    if os.name == "posix":
        os.chmod(active_path, 0o600)

    result = spool.append_records((_record("unit-fresh"),))
    receipt = result.unit_results[0].receipt

    quarantine_spools = sorted(quarantine_path.glob("*.spool"))
    quarantine_sidecars = sorted(quarantine_path.glob("*.json"))
    assert len(quarantine_spools) == 1
    assert len(quarantine_sidecars) == 1
    assert quarantine_spools[0].read_bytes() == original_bytes
    assert f"vp{len(clean_frame)}" in quarantine_spools[0].name

    meta = json.loads(quarantine_sidecars[0].read_text(encoding="utf-8"))
    assert meta["sequence"] == 1
    assert meta["tail_status"] == status.value
    assert meta["valid_prefix_bytes"] == len(clean_frame)
    assert meta["original_size"] == len(original_bytes)
    assert "quarantined_at" in meta

    scan = spool.scan_spool(active_path)
    assert scan.tail_status is spool.SpoolTailStatus.CLEAN
    assert scan.frame_count == 1
    assert receipt.offset == 0


def test_existing_active_append_does_not_require_directory_fsync(spool_home, monkeypatch):
    first = spool.append_records((_record("unit-a"),)).unit_results[0].receipt

    def _boom(_path: Path) -> None:  # pragma: no cover - should never fire
        raise AssertionError("directory fsync should not run for an existing active append")

    monkeypatch.setattr(spool, "_fsync_directory", _boom)
    second = spool.append_records((_record("unit-b"),)).unit_results[0].receipt

    assert second.offset == first.frame_length


def test_multi_unit_partial_append_returns_durable_prefix_only(spool_home, monkeypatch):
    real_write = spool.os.write
    second_unit = {"active": False, "started": False}

    def _flaky_write(fd: int, data: bytes) -> int:
        if not second_unit["started"]:
            if data.startswith(b"HSPL") and second_unit["active"]:
                second_unit["started"] = True
                wrote = real_write(fd, data[:7])
                raise OSError("interrupted write")
            if data.startswith(b"HSPL"):
                second_unit["active"] = True
        return real_write(fd, data)

    monkeypatch.setattr(spool.os, "write", _flaky_write)

    with pytest.raises(spool.SpoolAppendAttemptPartialError) as excinfo:
        spool.append_records((_record("unit-a"), _record("unit-b", attempt_index=1)))

    err = excinfo.value
    assert len(err.durable_results) == 1
    assert err.durable_results[0].persistence_unit_id == "unit-a"

    _, active_path, _, _ = _paths(spool_home)
    scan = spool.scan_spool(active_path)
    assert scan.valid_prefix_bytes == err.durable_results[0].receipt.frame_length
    assert scan.tail_status is spool.SpoolTailStatus.INCOMPLETE_EOF
