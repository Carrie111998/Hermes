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


def _capacity_artifact_payload(*, family: str) -> bytes:
    if family in {"clean_sealed_spool", "prefix_sealed_spool"}:
        return b"X" * 257
    if family == "ack_json":
        return json.dumps(
            {
                "acked_prefix_bytes": 5,
                "last_frame_checksum_hex": "1" * 32,
                "last_frame_length": 5,
                "last_frame_offset": 0,
                "schema_version": 1,
                "segment_kind": "clean",
                "segment_name": "00000000000000000001.spool",
                "segment_sequence": "00000000000000000001",
                "segment_size_bytes": 5,
                "tail_status": "clean",
                "valid_prefix_bytes": 5,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    if family == "blocker_json":
        return json.dumps(
            {
                "acked_prefix_bytes": 0,
                "blocking_offset": 0,
                "evidence_sidecar_name": "seq-00000000000000000001-invalid_json-vp0.json",
                "evidence_spool_name": "seq-00000000000000000001-invalid_json-vp0.spool",
                "original_size_bytes": 5,
                "prefix_segment_name": None,
                "schema_version": 1,
                "segment_sequence": "00000000000000000001",
                "source_kind": "sealed",
                "tail_status": "invalid_json",
                "valid_prefix_bytes": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    if family == "highwater_json":
        return json.dumps(
            {
                "last_reserved_sequence": "00000000000000000001",
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    if family in {"replay_quarantine_json", "legacy_quarantine_json"}:
        return json.dumps(
            {
                "original_size": 5,
                "quarantined_at": 1.0,
                "sequence": 1,
                "tail_status": "clean",
                "valid_prefix_bytes": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    if family in {"replay_quarantine_spool", "legacy_quarantine_spool"}:
        return b"Q" * 257
    raise AssertionError(f"unknown capacity artifact family: {family}")


@pytest.mark.parametrize(
    ("family", "artifact_parts"),
    [
        pytest.param(
            "clean_sealed_spool",
            (spool.SEALED_DIR_NAME, "00000000000000000001.spool"),
            id="clean_sealed_spool",
        ),
        pytest.param(
            "prefix_sealed_spool",
            (spool.SEALED_DIR_NAME, "00000000000000000001.prefix.spool"),
            id="prefix_sealed_spool",
        ),
        pytest.param(
            "ack_json",
            (
                spool.SEALED_DIR_NAME,
                spool.ACKS_DIR_NAME,
                "00000000000000000001.spool.ap00000000000000000005.json",
            ),
            id="ack_json",
        ),
        pytest.param(
            "blocker_json",
            (
                spool.SEALED_DIR_NAME,
                spool.BLOCKERS_DIR_NAME,
                "00000000000000000001.blocker.json",
            ),
            id="blocker_json",
        ),
        pytest.param(
            "highwater_json",
            (spool.HIGHWATER_FILE_NAME,),
            id="highwater_json",
        ),
        pytest.param(
            "replay_quarantine_spool",
            (spool.QUARANTINE_DIR_NAME, "seq-00000000000000000001-clean-vp0.spool"),
            id="replay_quarantine_spool",
        ),
        pytest.param(
            "replay_quarantine_json",
            (spool.QUARANTINE_DIR_NAME, "seq-00000000000000000001-clean-vp0.json"),
            id="replay_quarantine_json",
        ),
        pytest.param(
            "legacy_quarantine_spool",
            (spool.QUARANTINE_DIR_NAME, "000001-clean-vp0.spool"),
            id="legacy_quarantine_spool",
        ),
        pytest.param(
            "legacy_quarantine_json",
            (spool.QUARANTINE_DIR_NAME, "000001-clean-vp0.json"),
            id="legacy_quarantine_json",
        ),
    ],
)
def test_capacity_artifact_family_is_counted(
    spool_home,
    monkeypatch,
    family,
    artifact_parts,
):
    root, active_path, _, _ = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    active_path.write_bytes(b"")

    artifact_path = root.joinpath(*artifact_parts)
    artifact_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact_bytes = _capacity_artifact_payload(family=family)
    artifact_path.write_bytes(artifact_bytes)
    if family in {"replay_quarantine_spool", "legacy_quarantine_spool"}:
        artifact_path.with_suffix(".json").write_bytes(b"")

    if os.name == "posix":
        os.chmod(root, 0o700)
        os.chmod(active_path, 0o600)
        current = artifact_path.parent
        while current != root:
            os.chmod(current, 0o700)
            current = current.parent
        os.chmod(artifact_path, 0o600)
        if family in {"replay_quarantine_spool", "legacy_quarantine_spool"}:
            os.chmod(artifact_path.with_suffix(".json"), 0o600)

    requested_frame = spool._frame_bytes_for_record(_record(f"unit-{family}"))
    monkeypatch.setattr(
        spool,
        "TOTAL_CAP_BYTES",
        len(artifact_bytes) + len(requested_frame) - 1,
    )
    before = active_path.read_bytes()

    with pytest.raises(spool.SpoolCapacityError):
        spool.append_records((_record(f"unit-{family}"),))

    assert before == b""
    assert active_path.read_bytes() == b""


def test_capacity_inventory_excludes_lock_and_protocol_temp(spool_home, monkeypatch):
    root, active_path, lock_path, _ = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    active_path.write_bytes(b"")
    lock_path.write_bytes(b"L" * 4096)
    owner_lock_path = root / spool.REPLAY_OWNER_LOCK_NAME
    owner_lock_path.write_bytes(b"O" * 4096)
    protocol_temp = root / ".segment-sequence.highwater.json.123.456.tmp"
    highwater_payload = json.dumps(
        {
            "last_reserved_sequence": "00000000000000000001",
            "schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    protocol_temp.write_bytes(highwater_payload + (b" " * 5000))

    if os.name == "posix":
        os.chmod(root, 0o700)
        os.chmod(active_path, 0o600)
        os.chmod(lock_path, 0o600)
        os.chmod(owner_lock_path, 0o600)
        os.chmod(protocol_temp, 0o600)

    requested_frame = spool._frame_bytes_for_record(_record("unit-capacity-exclude"))
    monkeypatch.setattr(spool, "TOTAL_CAP_BYTES", len(requested_frame))

    result = spool.append_records((_record("unit-capacity-exclude"),))

    assert len(result.unit_results) == 1
    assert result.unit_results[0].receipt.offset == 0
    assert result.unit_results[0].receipt.frame_length == len(requested_frame)
    assert active_path.read_bytes() == requested_frame


@pytest.mark.parametrize(
    ("anomaly", "expected_exc", "match"),
    [
        pytest.param(
            "sealed_symlink",
            spool.SpoolPathSecurityError,
            "symlinked fallback spool path refused",
            id="sealed_symlink",
        ),
        pytest.param(
            "sealed_fifo",
            spool.SpoolPathSecurityError,
            "not a regular file",
            id="sealed_fifo",
            marks=pytest.mark.skipif(os.name != "posix", reason="FIFO anomaly is POSIX-only"),
        ),
        pytest.param(
            "sealed_unexpected_dir",
            spool.SpoolPathSecurityError,
            "unexpected fallback spool directory encountered during sequence inventory",
            id="sealed_unexpected_dir",
        ),
        pytest.param(
            "sealed_unrecognized_file",
            spool.SpoolDurabilityError,
            "unrecognized sealed segment artifact",
            id="sealed_unrecognized_file",
        ),
        pytest.param(
            "root_unrecognized_file",
            spool.SpoolDurabilityError,
            "unrecognized sequence-bearing artifact",
            id="root_unrecognized_file",
        ),
    ],
)
def test_capacity_inventory_fails_closed(spool_home, anomaly, expected_exc, match):
    root, active_path, _, _ = _paths(spool_home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    active_path.write_bytes(b"")

    sealed_dir = root / spool.SEALED_DIR_NAME
    sealed_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    anomaly_path = (
        root / "unexpected.bin"
        if anomaly == "root_unrecognized_file"
        else sealed_dir
        / (
            "unexpected"
            if anomaly == "sealed_unexpected_dir"
            else "unexpected.bin"
            if anomaly == "sealed_unrecognized_file"
            else "00000000000000000001.spool"
        )
    )

    if anomaly == "sealed_symlink":
        outside = spool_home / "outside-target.spool"
        outside.write_bytes(b"outside")
        anomaly_path.symlink_to(outside)
    elif anomaly == "sealed_fifo":
        os.mkfifo(anomaly_path)
    elif anomaly == "sealed_unexpected_dir":
        anomaly_path.mkdir(mode=0o700)
    else:
        anomaly_path.write_bytes(b"unexpected")

    if os.name == "posix":
        os.chmod(root, 0o700)
        os.chmod(active_path, 0o600)
        os.chmod(sealed_dir, 0o700)
        if anomaly in {"sealed_unexpected_dir"}:
            os.chmod(anomaly_path, 0o700)
        elif anomaly not in {"sealed_symlink", "sealed_fifo"}:
            os.chmod(anomaly_path, 0o600)

    before = active_path.read_bytes()

    with pytest.raises(expected_exc, match=match):
        spool.append_records((_record(f"unit-{anomaly}"),))

    assert before == b""
    assert active_path.read_bytes() == b""


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


def _close_runtime(runtime) -> None:
    spool._close_fd_quietly(runtime.lock_fd)
    spool._close_fd_quietly(runtime.root_fd)
    spool._close_fd_quietly(runtime.home_fd)


def test_replay_owner_lock_is_exclusive_and_supports_takeover_after_release(spool_home):
    first_runtime = spool._open_locked_runtime()
    second_runtime = spool._open_locked_runtime()
    try:
        owner = spool._try_acquire_replay_owner(first_runtime)
        assert owner is not None
        assert spool._try_acquire_replay_owner(second_runtime) is None

        spool._close_fd_quietly(owner.fd)
        takeover = spool._try_acquire_replay_owner(second_runtime)
        assert takeover is not None
        spool._close_fd_quietly(takeover.fd)
    finally:
        _close_runtime(second_runtime)
        _close_runtime(first_runtime)


def test_segment_sequence_highwater_never_reuses_reserved_values(spool_home):
    runtime = spool._open_locked_runtime()
    sealed_fd = -1
    try:
        with spool._append_lock(runtime.lock_fd, str(spool._lock_path())):
            sealed_fd, _ = spool._open_dir_at(
                runtime.root_fd,
                spool.SEALED_DIR_NAME,
                full_path=spool._sealed_dir(),
                mode=spool.ROOT_MODE,
                create=True,
                parent_label=runtime.root_path,
                fsync_parent_on_open_existing=True,
            )
            first = spool._allocate_next_segment_sequence(
                runtime=runtime,
                root_fd=runtime.root_fd,
            )
            assert first == 1
            (spool._sealed_dir() / f"{first:020d}.spool").write_bytes(b"reserved-gap")

            second = spool._allocate_next_segment_sequence(
                runtime=runtime,
                root_fd=runtime.root_fd,
            )
            assert second == 2

        highwater = json.loads(
            spool._segment_sequence_highwater_path().read_text(encoding="utf-8")
        )
        assert highwater == {
            "last_reserved_sequence": "00000000000000000002",
            "schema_version": 1,
        }
    finally:
        if sealed_fd >= 0:
            spool._close_fd_quietly(sealed_fd)
        _close_runtime(runtime)


def test_decode_spool_segment_returns_full_record_and_frame_metadata(spool_home):
    record = _record("unit-decode", contents=("hello", "world"))
    frame = spool._frame_bytes_for_record(record)
    segment_path = spool_home / "segment.spool"
    segment_path.write_bytes(frame)

    decoded = spool.decode_spool_segment(segment_path)

    assert decoded.valid_prefix_bytes == len(frame)
    assert decoded.tail_status is spool.SpoolTailStatus.CLEAN
    assert decoded.tail_offset is None
    assert len(decoded.prefix_frames) == 1

    decoded_frame = decoded.prefix_frames[0]
    assert decoded_frame.record == record
    assert decoded_frame.frame_offset == 0
    assert decoded_frame.frame_length == len(frame)
    assert decoded_frame.payload_length == len(frame) - spool.HEADER_SIZE
    assert decoded_frame.checksum_hex == frame[16:32].hex()


def test_allocate_next_segment_sequence_reconstructs_from_all_artifact_families(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        root = spool._spool_root()
        sealed_dir = spool._sealed_dir()
        acks_dir = spool._acks_dir()
        blockers_dir = spool._blockers_dir()
        quarantine_dir = spool._quarantine_dir()
        sealed_dir.mkdir(parents=True, exist_ok=True)
        acks_dir.mkdir(parents=True, exist_ok=True)
        blockers_dir.mkdir(parents=True, exist_ok=True)
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        (sealed_dir / "00000000000000000003.spool").write_bytes(b"clean")
        (sealed_dir / "00000000000000000004.prefix.spool").write_bytes(b"prefix")
        (acks_dir / "00000000000000000007.spool.ap00000000000000000099.json").write_text(
            "{}",
            encoding="utf-8",
        )
        (blockers_dir / "00000000000000000008.blocker.json").write_text("{}", encoding="utf-8")
        (quarantine_dir / "seq-00000000000000000009-checksum_mismatch-vp10.spool").write_bytes(
            b"evidence"
        )
        (quarantine_dir / "seq-00000000000000000010-invalid_json-vp0.json").write_text(
            "{}",
            encoding="utf-8",
        )
        (root / ".segment-sequence.highwater.json.123.456.tmp").write_text(
            json.dumps(
                {
                    "last_reserved_sequence": "00000000000000000011",
                    "schema_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        spool._segment_sequence_highwater_path().write_text(
            json.dumps(
                {
                    "last_reserved_sequence": "00000000000000000002",
                    "schema_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        with spool._append_lock(runtime.lock_fd, str(spool._lock_path())):
            next_sequence = spool._allocate_next_segment_sequence(
                runtime=runtime,
                root_fd=runtime.root_fd,
            )

        assert next_sequence == 12
    finally:
        _close_runtime(runtime)


def test_malformed_sequence_bearing_name_blocks_replay(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        sealed_dir = spool._sealed_dir()
        sealed_dir.mkdir(parents=True, exist_ok=True)
        (sealed_dir / "not-a-sequence.spool").write_bytes(b"bad")

        with spool._append_lock(runtime.lock_fd, str(spool._lock_path())):
            with pytest.raises(spool.SpoolDurabilityError, match="unrecognized sealed segment artifact"):
                spool._allocate_next_segment_sequence(
                    runtime=runtime,
                    root_fd=runtime.root_fd,
                )
    finally:
        _close_runtime(runtime)


def test_malformed_sequence_bearing_protocol_temp_blocks_replay(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        root = spool._spool_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / ".mystery-sequence-00000000000000000012.123.456.tmp").write_text(
            "bad-temp",
            encoding="utf-8",
        )

        with spool._append_lock(runtime.lock_fd, str(spool._lock_path())):
            with pytest.raises(spool.SpoolDurabilityError, match="unrecognized protocol temp artifact"):
                spool._allocate_next_segment_sequence(
                    runtime=runtime,
                    root_fd=runtime.root_fd,
                )
    finally:
        _close_runtime(runtime)


@pytest.mark.skipif(os.name != "posix", reason="directory-swap security checks are POSIX-only")
@pytest.mark.parametrize(
    ("target", "target_parts"),
    [
        ("root_swap", ()),
        ("sealed_swap", (spool.SEALED_DIR_NAME,)),
        ("acks_swap", (spool.SEALED_DIR_NAME, spool.ACKS_DIR_NAME)),
        ("blockers_swap", (spool.SEALED_DIR_NAME, spool.BLOCKERS_DIR_NAME)),
        ("quarantine_swap", (spool.QUARANTINE_DIR_NAME,)),
    ],
)
def test_replay_security_checks_reject_root_swap_for_sealed_acks_blockers_and_quarantine(
    spool_home,
    target,
    target_parts,
):
    runtime = spool._open_locked_runtime()
    try:
        root = spool._spool_root()
        sealed = spool._sealed_dir()
        acks = spool._acks_dir()
        blockers = spool._blockers_dir()
        quarantine = spool._quarantine_dir()
        sealed.mkdir(parents=True, exist_ok=True)
        acks.mkdir(parents=True, exist_ok=True)
        blockers.mkdir(parents=True, exist_ok=True)
        quarantine.mkdir(parents=True, exist_ok=True)

        external = spool_home / f"{target}-external"
        external.mkdir()
        target_path = root.joinpath(*target_parts) if target_parts else root
        parked = target_path.with_name(target_path.name + ".real")
        os.replace(target_path, parked)
        os.symlink(external, target_path, target_is_directory=True)

        with spool._append_lock(runtime.lock_fd, str(spool._lock_path())):
            with pytest.raises(spool.SpoolPathSecurityError):
                spool._allocate_next_segment_sequence(
                    runtime=runtime,
                    root_fd=runtime.root_fd,
                )

        assert not (external / spool.HIGHWATER_FILE_NAME).exists()
    finally:
        _close_runtime(runtime)


def test_inventory_fd_repeated_exceptions_return_to_baseline(spool_home):
    baseline = _fd_count()
    root = spool._spool_root()
    root.mkdir(parents=True, exist_ok=True)
    for attempt in range(10):
        runtime = spool._open_locked_runtime()
        temp_path = root / f".mystery-sequence-0000000000000000{attempt:04d}.123.456.tmp"
        temp_path.write_text("bad-temp", encoding="utf-8")
        try:
            with spool._append_lock(runtime.lock_fd, str(spool._lock_path())):
                with pytest.raises(spool.SpoolDurabilityError, match="unrecognized protocol temp artifact"):
                    spool._allocate_next_segment_sequence(
                        runtime=runtime,
                        root_fd=runtime.root_fd,
                    )
        finally:
            _close_runtime(runtime)
            temp_path.unlink(missing_ok=True)
    assert _fd_count() == baseline


def _ack_payload(*, segment_sequence: int, segment_name: str, acked_prefix_bytes: int, valid_prefix_bytes: int, tail_status: str = "clean", segment_kind: str = "clean"):
    return {
        "schema_version": 1,
        "segment_sequence": f"{segment_sequence:020d}",
        "segment_name": segment_name,
        "segment_kind": segment_kind,
        "segment_size_bytes": valid_prefix_bytes,
        "acked_prefix_bytes": acked_prefix_bytes,
        "valid_prefix_bytes": valid_prefix_bytes,
        "tail_status": tail_status,
        "last_frame_offset": 0,
        "last_frame_length": acked_prefix_bytes,
        "last_frame_checksum_hex": "1" * 32,
    }


def test_publish_ack_same_prefix_same_content_is_idempotent_success(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        sealed_dir = spool._sealed_dir()
        acks_dir = spool._acks_dir()
        sealed_dir.mkdir(parents=True, exist_ok=True)
        acks_dir.mkdir(parents=True, exist_ok=True)
        segment_path = sealed_dir / "00000000000000000001.spool"
        segment_path.write_bytes(b"alpha")
        payload = _ack_payload(
            segment_sequence=1,
            segment_name=segment_path.name,
            acked_prefix_bytes=5,
            valid_prefix_bytes=5,
        )

        spool._publish_ack_sidecar_strict(
            runtime,
            segment_sequence=1,
            segment_path=segment_path,
            ack_payload=payload,
        )
        spool._publish_ack_sidecar_strict(
            runtime,
            segment_sequence=1,
            segment_path=segment_path,
            ack_payload=payload,
        )

        assert sorted(acks_dir.glob("*.json")) == [acks_dir / "00000000000000000001.spool.ap00000000000000000005.json"]
    finally:
        _close_runtime(runtime)


def test_publish_ack_same_prefix_different_content_blocks_integrity(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        sealed_dir = spool._sealed_dir()
        acks_dir = spool._acks_dir()
        sealed_dir.mkdir(parents=True, exist_ok=True)
        acks_dir.mkdir(parents=True, exist_ok=True)
        segment_path = sealed_dir / "00000000000000000001.spool"
        segment_path.write_bytes(b"alpha")
        spool._write_sidecar_json(
            acks_dir / "00000000000000000001.spool.ap00000000000000000005.json",
            _ack_payload(
                segment_sequence=1,
                segment_name=segment_path.name,
                acked_prefix_bytes=5,
                valid_prefix_bytes=5,
            ),
        )

        with pytest.raises(spool.SpoolDurabilityError, match="conflicting ack sidecar"):
            spool._publish_ack_sidecar_strict(
                runtime,
                segment_sequence=1,
                segment_path=segment_path,
                ack_payload=_ack_payload(
                    segment_sequence=1,
                    segment_name=segment_path.name,
                    acked_prefix_bytes=5,
                    valid_prefix_bytes=5,
                    tail_status="checksum_mismatch",
                ),
            )
    finally:
        _close_runtime(runtime)


def test_malformed_or_oversized_ack_sidecar_blocks_integrity(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        sealed_dir = spool._sealed_dir()
        acks_dir = spool._acks_dir()
        sealed_dir.mkdir(parents=True, exist_ok=True)
        acks_dir.mkdir(parents=True, exist_ok=True)
        segment_path = sealed_dir / "00000000000000000001.spool"
        segment_path.write_bytes(b"alpha")
        malformed = acks_dir / "00000000000000000001.spool.ap00000000000000000005.json"
        malformed.write_text("{}", encoding="utf-8")

        with pytest.raises(spool.SpoolDurabilityError, match="invalid ack sidecar"):
            spool._load_ack_sidecar_winner(runtime=runtime, segment_path=segment_path)

        malformed.unlink()
        oversized = acks_dir / "00000000000000000001.spool.ap00000000000000000005.json"
        oversized.write_text("x" * 3000, encoding="utf-8")
        with pytest.raises(spool.SpoolDurabilityError, match="invalid ack sidecar"):
            spool._load_ack_sidecar_winner(runtime=runtime, segment_path=segment_path)
    finally:
        _close_runtime(runtime)


def test_highest_valid_ack_winner_ignores_lower_or_mismatched_sidecars(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        sealed_dir = spool._sealed_dir()
        acks_dir = spool._acks_dir()
        sealed_dir.mkdir(parents=True, exist_ok=True)
        acks_dir.mkdir(parents=True, exist_ok=True)
        segment_path = sealed_dir / "00000000000000000001.spool"
        segment_path.write_bytes(b"alpha")
        spool._write_sidecar_json(
            acks_dir / "00000000000000000001.spool.ap00000000000000000003.json",
            _ack_payload(
                segment_sequence=1,
                segment_name=segment_path.name,
                acked_prefix_bytes=3,
                valid_prefix_bytes=5,
            ),
        )
        spool._write_sidecar_json(
            acks_dir / "00000000000000000001.spool.ap00000000000000000005.json",
            _ack_payload(
                segment_sequence=1,
                segment_name=segment_path.name,
                acked_prefix_bytes=5,
                valid_prefix_bytes=5,
            ),
        )
        spool._write_sidecar_json(
            acks_dir / "00000000000000000009.spool.ap00000000000000000099.json",
            _ack_payload(
                segment_sequence=9,
                segment_name="00000000000000000009.spool",
                acked_prefix_bytes=99,
                valid_prefix_bytes=99,
            ),
        )

        winner = spool._load_ack_sidecar_winner(runtime=runtime, segment_path=segment_path)

        assert winner is not None
        assert winner["acked_prefix_bytes"] == 5
    finally:
        _close_runtime(runtime)


def test_ack_sidecar_count_above_64_blocks_integrity(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        sealed_dir = spool._sealed_dir()
        acks_dir = spool._acks_dir()
        sealed_dir.mkdir(parents=True, exist_ok=True)
        acks_dir.mkdir(parents=True, exist_ok=True)
        segment_path = sealed_dir / "00000000000000000001.spool"
        segment_path.write_bytes(b"a" * 65)
        for idx in range(1, 66):
            spool._write_sidecar_json(
                acks_dir / f"00000000000000000001.spool.ap{idx:020d}.json",
                _ack_payload(
                    segment_sequence=1,
                    segment_name=segment_path.name,
                    acked_prefix_bytes=idx,
                    valid_prefix_bytes=65,
                ),
            )

        with pytest.raises(spool.SpoolDurabilityError, match="too many ack sidecars"):
            spool._load_ack_sidecar_winner(runtime=runtime, segment_path=segment_path)
    finally:
        _close_runtime(runtime)


def test_replay_to_session_db_uses_strict_ack_publication(spool_home, monkeypatch):
    calls = []

    def _strict(runtime, *, segment_sequence, segment_path, ack_payload):
        calls.append(
            {
                "runtime": runtime,
                "segment_sequence": segment_sequence,
                "segment_path": segment_path,
                "ack_payload": ack_payload,
            }
        )

    monkeypatch.setattr(spool, "_publish_ack_sidecar_strict", _strict)
    monkeypatch.setattr(spool, "_delete_fully_acked_segment", lambda *_args, **_kwargs: None)

    from hermes_state import SessionDB

    db = SessionDB(db_path=spool_home / "state.db")
    try:
        monkeypatch.setenv("HERMES_HOME", str(spool_home / ".hermes"))
        hermes_home = spool_home / ".hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        sealed_dir = hermes_home / spool.SPOOL_ROOT_NAME / spool.SEALED_DIR_NAME
        sealed_dir.mkdir(parents=True, exist_ok=True)
        segment_path = sealed_dir / "00000000000000000001.spool"
        segment_path.write_bytes(spool._frame_bytes_for_record(_record()))

        result = spool.replay_to_session_db(db, trigger="startup")

        assert result.state is spool.ReplayRunState.REPLAYED
        assert len(calls) == 1
        assert calls[0]["segment_sequence"] == 1
        assert calls[0]["segment_path"].name == "00000000000000000001.spool"
    finally:
        db.close()


def test_stale_lower_ack_sidecars_cleanup_only_after_durable_higher_winner(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        sealed_dir = spool._sealed_dir()
        acks_dir = spool._acks_dir()
        sealed_dir.mkdir(parents=True, exist_ok=True)
        acks_dir.mkdir(parents=True, exist_ok=True)
        segment_path = sealed_dir / "00000000000000000001.spool"
        segment_path.write_bytes(b"alpha")
        spool._write_sidecar_json(
            acks_dir / "00000000000000000001.spool.ap00000000000000000003.json",
            _ack_payload(
                segment_sequence=1,
                segment_name=segment_path.name,
                acked_prefix_bytes=3,
                valid_prefix_bytes=5,
            ),
        )

        spool._publish_ack_sidecar_strict(
            runtime,
            segment_sequence=1,
            segment_path=segment_path,
            ack_payload=_ack_payload(
                segment_sequence=1,
                segment_name=segment_path.name,
                acked_prefix_bytes=5,
                valid_prefix_bytes=5,
            ),
        )

        assert sorted(path.name for path in acks_dir.glob("*.json")) == [
            "00000000000000000001.spool.ap00000000000000000005.json"
        ]
    finally:
        _close_runtime(runtime)


def test_corrupt_active_with_valid_prefix_publishes_prefix_evidence_and_blocker(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        root = spool._spool_root()
        sealed_dir = spool._sealed_dir()
        blockers_dir = spool._blockers_dir()
        quarantine_dir = spool._quarantine_dir()
        root.mkdir(parents=True, exist_ok=True)
        sealed_dir.mkdir(parents=True, exist_ok=True)
        blockers_dir.mkdir(parents=True, exist_ok=True)
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        clean_frame = spool._frame_bytes_for_record(_record("unit-clean"))
        corrupt_frame = bytearray(spool._frame_bytes_for_record(_record("unit-bad", attempt_index=1)))
        corrupt_frame[-1] ^= 0x01
        active_path = root / spool.ACTIVE_SPOOL_NAME
        active_path.write_bytes(clean_frame + bytes(corrupt_frame))

        runtime = spool._open_locked_runtime()
        with spool._append_lock(runtime.lock_fd, str(spool._lock_path())):
            result = spool._reconcile_active_spool_for_replay(runtime)

        assert result["tail_status"] is spool.SpoolTailStatus.CHECKSUM_MISMATCH
        assert result["valid_prefix_bytes"] == len(clean_frame)
        assert result["segment_sequence"] == 1
        assert result["prefix_segment_name"] == "00000000000000000001.prefix.spool"
        assert (sealed_dir / "00000000000000000001.prefix.spool").read_bytes() == clean_frame
        evidence_spools = sorted(quarantine_dir.glob("seq-00000000000000000001-*.spool"))
        assert len(evidence_spools) == 1
        assert evidence_spools[0].read_bytes() == clean_frame + bytes(corrupt_frame)
        blocker = json.loads((blockers_dir / "00000000000000000001.blocker.json").read_text(encoding="utf-8"))
        assert blocker["prefix_segment_name"] == "00000000000000000001.prefix.spool"
        assert blocker["blocking_offset"] == len(clean_frame)
        assert active_path.exists()
        assert active_path.read_bytes() == b""
    finally:
        _close_runtime(runtime)


def test_corrupt_active_with_zero_prefix_publishes_evidence_and_blocker(spool_home):
    runtime = spool._open_locked_runtime()
    try:
        root = spool._spool_root()
        blockers_dir = spool._blockers_dir()
        quarantine_dir = spool._quarantine_dir()
        root.mkdir(parents=True, exist_ok=True)
        blockers_dir.mkdir(parents=True, exist_ok=True)
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        corrupt_frame = bytearray(spool._frame_bytes_for_record(_record("unit-bad")))
        corrupt_frame[0] = 0x00
        active_path = root / spool.ACTIVE_SPOOL_NAME
        active_path.write_bytes(bytes(corrupt_frame))

        runtime = spool._open_locked_runtime()
        with spool._append_lock(runtime.lock_fd, str(spool._lock_path())):
            result = spool._reconcile_active_spool_for_replay(runtime)

        assert result["tail_status"] is spool.SpoolTailStatus.BAD_MAGIC
        assert result["valid_prefix_bytes"] == 0
        assert result["segment_sequence"] == 1
        assert result["prefix_segment_name"] is None
        evidence_spools = sorted(quarantine_dir.glob("seq-00000000000000000001-*.spool"))
        assert len(evidence_spools) == 1
        assert evidence_spools[0].read_bytes() == bytes(corrupt_frame)
        blocker = json.loads((blockers_dir / "00000000000000000001.blocker.json").read_text(encoding="utf-8"))
        assert blocker["prefix_segment_name"] is None
        assert blocker["blocking_offset"] == 0
        assert active_path.exists()
        assert active_path.read_bytes() == b""
    finally:
        _close_runtime(runtime)
