from __future__ import annotations

import inspect
import multiprocessing
import os
import queue
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest

from scripts.canary import production_release_active_transaction as registry
from scripts.canary import production_release_update_runtime as runtime
from tests.scripts.canary.test_production_release_update_contract import (
    _documents,
)
from tests.scripts.canary.test_production_release_update_runtime import (
    _authority_record,
)


class SimulatedCrash(BaseException):
    pass


def _concurrent_create_worker(
    root: str,
    authority_record: Mapping[str, Any],
    all_workers_ready: Any,
    hold_pending: bool,
    pending_live: Any,
    release_holder: Any,
    lock_attempts: Any,
    results: Any,
) -> None:
    if hold_pending:
        def hold_live_pending(name: str) -> None:
            if name == "active_pending_created":
                pending_live.set()
                release_holder.wait(20)

        setattr(registry, "_checkpoint", hold_live_pending)
    else:
        original_flock = registry.fcntl.flock

        def prove_lock_contention(
            descriptor: int,
            operation: int,
        ) -> None:
            if operation != registry.fcntl.LOCK_EX:
                original_flock(descriptor, operation)
                return
            try:
                original_flock(
                    descriptor,
                    registry.fcntl.LOCK_EX | registry.fcntl.LOCK_NB,
                )
            except BlockingIOError:
                lock_attempts.put("blocked")
                original_flock(descriptor, operation)
                return
            original_flock(descriptor, registry.fcntl.LOCK_UN)
            lock_attempts.put("uncontended")
            original_flock(descriptor, operation)

        setattr(
            registry.fcntl,
            "flock",
            prove_lock_contention,
        )
    all_workers_ready.wait(timeout=20)
    if not hold_pending:
        pending_live.wait(20)
    try:
        marker = registry._create_or_replay_for_test(
            Path(root),
            authority_record=authority_record,
        )
        results.put(("ok", marker["intent_sha256"]))
    except Exception as exc:
        results.put(("error", str(exc)))


def _blocking_writer_worker(
    root: str,
    authority_record: Mapping[str, Any],
    entered: Any,
    release: Any,
    results: Any,
) -> None:
    def hold_pending(name: str) -> None:
        if name == "active_pending_written":
            entered.set()
            release.wait(10)

    registry._checkpoint = hold_pending
    try:
        marker = registry._create_or_replay_for_test(
            Path(root),
            authority_record=authority_record,
        )
        results.put(("writer", "ok", marker["intent_sha256"]))
    except Exception as exc:
        results.put(("writer", "error", str(exc)))


def _concurrent_read_worker(
    root: str,
    ready: Any,
    lock_blocked: Any,
    results: Any,
) -> None:
    original_flock = registry.fcntl.flock

    def prove_lock_contention(
        descriptor: int,
        operation: int,
    ) -> None:
        if operation != registry.fcntl.LOCK_EX:
            original_flock(descriptor, operation)
            return
        try:
            original_flock(
                descriptor,
                registry.fcntl.LOCK_EX | registry.fcntl.LOCK_NB,
            )
        except BlockingIOError:
            lock_blocked.set()
            original_flock(descriptor, operation)
            return
        original_flock(descriptor, registry.fcntl.LOCK_UN)
        original_flock(descriptor, operation)

    setattr(registry.fcntl, "flock", prove_lock_contention)
    ready.set()
    try:
        marker = registry._read_for_test(Path(root))
        results.put(("reader", "ok", marker["intent_sha256"]))
    except Exception as exc:
        results.put(("reader", "error", str(exc)))


def _root(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    return (tmp_path / "registry").resolve()


def _other_authority_record() -> Mapping[str, Any]:
    _private, trusted, plan, _approval, publication = _documents()
    return runtime.build_authority_record(
        publication=publication,
        trusted_predecessor=trusted,
        expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
        predecessor_current_receipt_sha256=str(
            plan["predecessor_activation_receipt_sha256"]
        ),
    )


def _marker_path(root: Path) -> Path:
    return root / registry.ACTIVE_MARKER_NAME


def _pending_path(root: Path) -> Path:
    return root / registry.ACTIVE_PENDING_NAME


def _create(root: Path) -> Mapping[str, Any]:
    return registry._create_or_replay_for_test(
        root,
        authority_record=_authority_record(),
    )


def test_create_read_and_exact_replay_are_canonical_and_immutable(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)

    created = _create(root)
    marker = _marker_path(root)
    first_status = marker.stat()
    replayed = _create(root)
    readback = registry._read_for_test(root)

    assert created == replayed == readback
    assert created["schema"] == registry.ACTIVE_MARKER_SCHEMA
    assert created["authority_record"] == _authority_record()
    assert created["authority_record_sha256"] == (
        _authority_record()["authority_record_sha256"]
    )
    assert created["intent_sha256"] == (
        _authority_record()["intent"]["intent_sha256"]
    )
    assert marker.read_bytes() == registry.canonical_json_bytes(created)
    assert stat.S_IMODE(root.stat().st_mode) == registry.DIRECTORY_MODE
    assert root.stat().st_uid == registry._posix_identity(
        "geteuid",
        failure_code="release_active_transaction_configuration_invalid",
    )
    assert root.stat().st_gid == tmp_path.stat().st_gid
    assert stat.S_IMODE(marker.stat().st_mode) == registry.FILE_MODE
    assert marker.stat().st_uid == registry._posix_identity(
        "geteuid",
        failure_code="release_active_transaction_configuration_invalid",
    )
    assert marker.stat().st_gid == tmp_path.stat().st_gid
    assert marker.stat().st_nlink == 1
    assert (marker.stat().st_dev, marker.stat().st_ino) == (
        first_status.st_dev,
        first_status.st_ino,
    )
    assert sorted(path.name for path in root.iterdir()) == [
        registry.ACTIVE_MARKER_NAME
    ]

    created["authority_record"]["intent"]["schema"] = "caller-mutated"
    assert registry._read_for_test(root)["authority_record"] == (
        _authority_record()
    )


def test_different_intent_conflicts_without_replacing_active_marker(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    first = _create(root)
    before = _marker_path(root).read_bytes()

    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_conflict$",
    ):
        registry._create_or_replay_for_test(
            root,
            authority_record=_other_authority_record(),
        )

    assert _marker_path(root).read_bytes() == before
    assert registry._read_for_test(root) == first


def _run_concurrent_creates(
    root: Path,
    authorities: list[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    context = multiprocessing.get_context("spawn")
    all_workers_ready = context.Barrier(len(authorities) + 1)
    pending_live = context.Event()
    release_holder = context.Event()
    lock_attempts = context.Queue()
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_create_worker,
            args=(
                str(root),
                deepcopy(authority),
                all_workers_ready,
                index == 0,
                pending_live,
                release_holder,
                lock_attempts,
                results,
            ),
        )
        for index, authority in enumerate(authorities)
    ]
    for process in processes:
        process.start()
    observed: list[tuple[str, str]] = []
    try:
        all_workers_ready.wait(timeout=20)
        assert pending_live.wait(timeout=20)
        for _contender in processes[1:]:
            assert lock_attempts.get(timeout=20) == "blocked"
        release_holder.set()
        for _process in processes:
            observed.append(results.get(timeout=20))
    finally:
        release_holder.set()
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)
    return observed


def test_concurrent_same_authority_creates_converge_exactly(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    authority = _authority_record()

    observed = _run_concurrent_creates(
        root,
        [authority for _index in range(6)],
    )

    assert observed == [
        ("ok", authority["intent"]["intent_sha256"])
    ] * 6
    assert registry._read_for_test(root)["authority_record"] == authority
    assert _marker_path(root).stat().st_nlink == 1
    assert not _pending_path(root).exists()


def test_concurrent_different_authorities_select_one_and_conflict_other(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    first = _authority_record()
    second = _other_authority_record()

    observed = _run_concurrent_creates(root, [first, second])

    successes = [value for status, value in observed if status == "ok"]
    failures = [value for status, value in observed if status == "error"]
    assert len(successes) == 1
    assert failures == ["release_active_transaction_conflict"]
    assert registry._read_for_test(root)["intent_sha256"] == successes[0]
    assert _marker_path(root).stat().st_nlink == 1
    assert not _pending_path(root).exists()


def test_read_waits_for_inflight_publication_and_observes_final_marker(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    authority = _authority_record()
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    reader_ready = context.Event()
    reader_lock_blocked = context.Event()
    writer_results = context.Queue()
    reader_results = context.Queue()
    writer = context.Process(
        target=_blocking_writer_worker,
        args=(
            str(root),
            deepcopy(authority),
            entered,
            release,
            writer_results,
        ),
    )
    reader = context.Process(
        target=_concurrent_read_worker,
        args=(
            str(root),
            reader_ready,
            reader_lock_blocked,
            reader_results,
        ),
    )
    writer.start()
    assert entered.wait(timeout=20)
    reader.start()
    try:
        assert reader_ready.wait(timeout=20)
        assert reader_lock_blocked.wait(timeout=20)
        with pytest.raises(queue.Empty):
            reader_results.get_nowait()
        assert reader.is_alive()
        release.set()
        observed = {
            writer_results.get(timeout=20),
            reader_results.get(timeout=20),
        }
    finally:
        release.set()
        for process in (writer, reader):
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    expected_intent = authority["intent"]["intent_sha256"]
    assert observed == {
        ("writer", "ok", expected_intent),
        ("reader", "ok", expected_intent),
    }
    assert writer.exitcode == reader.exitcode == 0


def test_root_directory_lock_failure_is_secret_free_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)

    def unavailable(_descriptor: int, _operation: int) -> None:
        raise OSError("sensitive kernel detail")

    monkeypatch.setattr(registry.fcntl, "flock", unavailable)
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_lock_unavailable$",
    ) as raised:
        _create(root)
    assert "sensitive" not in str(raised.value)


def test_secure_parent_namespace_churn_does_not_fake_path_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    original_open = registry.os.open
    changed = False

    def change_parent_then_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal changed
        if (
            not changed
            and dir_fd is None
            and Path(path) == root.parent
        ):
            changed = True
            (tmp_path / "unrelated-secure-child").mkdir(mode=0o700)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(registry.os, "open", change_parent_then_open)

    assert _create(root)["authority_record"] == _authority_record()


def test_live_parent_churn_after_held_fstat_preserves_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    root.mkdir(mode=registry.DIRECTORY_MODE)
    root.chmod(registry.DIRECTORY_MODE)
    instance = registry._ActiveTransactionRegistry(
        root=root,
        require_root=False,
        xattr_reader=lambda _descriptor: (),
    )
    opened = instance._open_root(create=False)
    assert opened is not None
    parent_fd, root_fd = opened
    parent_status = root.parent.stat()
    original_fstat = registry.os.fstat
    changed = False

    def churn_after_parent_fstat(descriptor: int) -> os.stat_result:
        nonlocal changed
        status = original_fstat(descriptor)
        if (
            not changed
            and (status.st_dev, status.st_ino)
            == (parent_status.st_dev, parent_status.st_ino)
        ):
            changed = True
            (tmp_path / "live-parent-child").mkdir(mode=0o700)
        return status

    monkeypatch.setattr(registry.os, "fstat", churn_after_parent_fstat)
    try:
        instance._verify_binding(parent_fd, root_fd)
    finally:
        os.close(root_fd)
        os.close(parent_fd)

    assert changed


def test_live_known_namespace_churn_preserves_stable_inode_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _create(root)
    transactions = root / "transactions"
    transactions.mkdir(mode=0o700)
    transactions.chmod(0o700)
    transactions_status = transactions.stat()
    original_fstat = registry.os.fstat
    changed = False

    def churn_after_sibling_fstat(descriptor: int) -> os.stat_result:
        nonlocal changed
        status = original_fstat(descriptor)
        if (
            not changed
            and (status.st_dev, status.st_ino)
            == (transactions_status.st_dev, transactions_status.st_ino)
        ):
            changed = True
            (transactions / "live-child").mkdir(mode=0o700)
        return status

    monkeypatch.setattr(registry.os, "fstat", churn_after_sibling_fstat)

    assert registry._read_for_test(root)["authority_record"] == (
        _authority_record()
    )
    assert changed


def test_public_boundaries_are_fixed_and_have_no_override_surface() -> None:
    assert registry.PRODUCTION_REGISTRY_ROOT == Path(
        "/var/lib/muncho-production-release-update"
    )
    assert list(
        inspect.signature(
            registry.create_or_replay_active_transaction
        ).parameters
    ) == ["authority_record"]
    assert (
        list(
            inspect.signature(
                registry.read_active_transaction
            ).parameters
        )
        == []
    )
    assert "root" not in registry.__all__
    assert "_create_or_replay_for_test" not in registry.__all__
    assert "_read_for_test" not in registry.__all__
    assert not any("retire" in name for name in registry.__all__)


@pytest.mark.parametrize("attribute", ["geteuid", "getegid"])
def test_production_boundary_requires_linux_root_identity(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
) -> None:
    monkeypatch.setattr(registry.sys, "platform", "linux")
    monkeypatch.setattr(
        registry.os,
        attribute,
        lambda: 1,
    )

    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_root_required$",
    ):
        registry.create_or_replay_active_transaction(
            authority_record=_authority_record(),
        )


def test_private_test_seam_requires_an_absolute_root() -> None:
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_configuration_invalid$",
    ):
        registry._create_or_replay_for_test(
            Path("relative"),
            authority_record=_authority_record(),
        )


def test_existing_only_read_never_creates_missing_root_or_marker(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    before = sorted(tmp_path.iterdir())

    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_not_found$",
    ):
        registry._read_for_test(root)

    assert sorted(tmp_path.iterdir()) == before
    assert not root.exists()

    root.mkdir(mode=registry.DIRECTORY_MODE)
    root.chmod(registry.DIRECTORY_MODE)
    before_status = root.stat()
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_not_found$",
    ):
        registry._read_for_test(root)
    after_status = root.stat()

    assert list(root.iterdir()) == []
    assert (after_status.st_dev, after_status.st_ino) == (
        before_status.st_dev,
        before_status.st_ino,
    )


@pytest.mark.parametrize(
    "boundary",
    registry.PUBLICATION_DURABLE_BOUNDARIES,
)
def test_publication_recovers_from_every_durable_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    root = _root(tmp_path)

    def crash(name: str) -> None:
        if name == boundary:
            raise SimulatedCrash

    monkeypatch.setattr(registry, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        _create(root)

    monkeypatch.setattr(registry, "_checkpoint", lambda _name: None)
    recovered = _create(root)

    assert recovered["authority_record"] == _authority_record()
    assert _marker_path(root).stat().st_nlink == 1
    assert not _pending_path(root).exists()
    assert registry._read_for_test(root) == recovered


@pytest.mark.parametrize(
    "boundary",
    registry.UNCOMMITTED_RECOVERY_DURABLE_BOUNDARIES,
)
def test_uncommitted_pending_recovery_retries_every_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    root = _root(tmp_path)

    def crash_publication(name: str) -> None:
        if name == "active_pending_directory_fsynced":
            raise SimulatedCrash

    monkeypatch.setattr(registry, "_checkpoint", crash_publication)
    with pytest.raises(SimulatedCrash):
        _create(root)
    assert _pending_path(root).exists()
    assert _pending_path(root).stat().st_nlink == 1
    assert not _marker_path(root).exists()

    def crash_recovery(name: str) -> None:
        if name == boundary:
            raise SimulatedCrash

    monkeypatch.setattr(registry, "_checkpoint", crash_recovery)
    with pytest.raises(SimulatedCrash):
        _create(root)

    monkeypatch.setattr(registry, "_checkpoint", lambda _name: None)
    recovered = _create(root)
    assert recovered["authority_record"] == _authority_record()
    assert _marker_path(root).stat().st_nlink == 1
    assert not _pending_path(root).exists()


@pytest.mark.parametrize(
    "boundary",
    registry.LINKED_RECOVERY_DURABLE_BOUNDARIES,
)
def test_linked_pending_recovery_retries_every_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    root = _root(tmp_path)

    def crash_publication(name: str) -> None:
        if name == "active_final_linked":
            raise SimulatedCrash

    monkeypatch.setattr(registry, "_checkpoint", crash_publication)
    with pytest.raises(SimulatedCrash):
        _create(root)
    assert _pending_path(root).stat().st_ino == (
        _marker_path(root).stat().st_ino
    )
    assert _pending_path(root).stat().st_nlink == 2

    def crash_recovery(name: str) -> None:
        if name == boundary:
            raise SimulatedCrash

    monkeypatch.setattr(registry, "_checkpoint", crash_recovery)
    with pytest.raises(SimulatedCrash):
        _create(root)

    monkeypatch.setattr(registry, "_checkpoint", lambda _name: None)
    recovered = _create(root)
    assert recovered["authority_record"] == _authority_record()
    assert _marker_path(root).stat().st_nlink == 1
    assert not _pending_path(root).exists()


def test_read_is_nonmutating_for_crash_residue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unpublished_parent = tmp_path / "unpublished"
    unpublished_parent.mkdir(mode=0o700)
    unpublished_root = _root(unpublished_parent)

    def crash_unpublished(name: str) -> None:
        if name == "active_pending_directory_fsynced":
            raise SimulatedCrash

    monkeypatch.setattr(registry, "_checkpoint", crash_unpublished)
    with pytest.raises(SimulatedCrash):
        _create(unpublished_root)
    pending_before = _pending_path(unpublished_root).stat()

    monkeypatch.setattr(registry, "_checkpoint", lambda _name: None)
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_recovery_required$",
    ):
        registry._read_for_test(unpublished_root)
    pending_after = _pending_path(unpublished_root).stat()
    assert (pending_before.st_dev, pending_before.st_ino) == (
        pending_after.st_dev,
        pending_after.st_ino,
    )
    assert not _marker_path(unpublished_root).exists()

    linked_parent = tmp_path / "linked"
    linked_parent.mkdir(mode=0o700)
    linked_root = _root(linked_parent)

    def crash_linked(name: str) -> None:
        if name == "active_final_linked":
            raise SimulatedCrash

    monkeypatch.setattr(registry, "_checkpoint", crash_linked)
    with pytest.raises(SimulatedCrash):
        _create(linked_root)
    linked_inode = _marker_path(linked_root).stat().st_ino

    monkeypatch.setattr(registry, "_checkpoint", lambda _name: None)
    observed = registry._read_for_test(linked_root)
    assert observed["authority_record"] == _authority_record()
    assert _pending_path(linked_root).exists()
    assert _pending_path(linked_root).stat().st_ino == linked_inode
    assert _marker_path(linked_root).stat().st_nlink == 2


def test_closed_inventory_accepts_only_valid_known_namespaces(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _create(root)
    transactions = root / "transactions"
    transactions.mkdir(mode=0o700)
    transactions.chmod(0o700)

    assert registry._read_for_test(root)["authority_record"] == (
        _authority_record()
    )

    (root / "unexpected").mkdir(mode=0o700)
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_inventory_invalid$",
    ):
        registry._read_for_test(root)


def test_known_namespace_must_be_secure_directory(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _create(root)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (root / "authority").symlink_to(target, target_is_directory=True)

    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_inventory_invalid$",
    ):
        registry._read_for_test(root)


def test_root_and_marker_modes_are_bound_and_fail_closed(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _create(root)
    _marker_path(root).chmod(0o600)

    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_file_invalid$",
    ):
        registry._read_for_test(root)

    _marker_path(root).chmod(registry.FILE_MODE)
    root.chmod(0o755)
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_directory_invalid$",
    ):
        registry._read_for_test(root)


def test_symlink_hardlink_and_extended_metadata_fail_closed(
    tmp_path: Path,
) -> None:
    hardlink_parent = tmp_path / "hardlink"
    hardlink_parent.mkdir(mode=0o700)
    hardlink_parent.chmod(0o700)
    hardlink_root = _root(hardlink_parent)
    _create(hardlink_root)
    os.link(
        _marker_path(hardlink_root),
        hardlink_parent / "outside-link",
    )
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_file_invalid$",
    ):
        registry._read_for_test(hardlink_root)

    symlink_parent = tmp_path / "symlink"
    symlink_parent.mkdir(mode=0o700)
    symlink_parent.chmod(0o700)
    symlink_root = _root(symlink_parent)
    _create(symlink_root)
    marker_raw = _marker_path(symlink_root).read_bytes()
    _marker_path(symlink_root).unlink()
    target = symlink_parent / "marker-target"
    target.write_bytes(marker_raw)
    target.chmod(registry.FILE_MODE)
    _marker_path(symlink_root).symlink_to(target)
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_file_invalid$",
    ):
        registry._read_for_test(symlink_root)

    metadata_parent = tmp_path / "metadata"
    metadata_parent.mkdir(mode=0o700)
    metadata_parent.chmod(0o700)
    metadata_root = _root(metadata_parent)
    _create(metadata_root)

    def xattrs(descriptor: int) -> tuple[str, ...]:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            return ("user.injected",)
        return ()

    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_extended_metadata_invalid$",
    ):
        registry._read_for_test(
            metadata_root,
            xattr_reader=xattrs,
        )


@pytest.mark.parametrize(
    "payload",
    (
        b'{ "not":"canonical" }',
        b'{"duplicate":1,"duplicate":2}',
        b'{"value":NaN}',
        b"\xff",
    ),
)
def test_noncanonical_or_invalid_json_fails_closed(
    tmp_path: Path,
    payload: bytes,
) -> None:
    root = _root(tmp_path)
    _create(root)
    marker = _marker_path(root)
    marker.chmod(0o600)
    marker.write_bytes(payload)
    marker.chmod(registry.FILE_MODE)

    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_json_invalid$",
    ):
        registry._read_for_test(root)


def test_canonical_marker_tamper_fails_validation_without_secret_echo(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    marker_value = _create(root)
    tampered = deepcopy(marker_value)
    tampered["intent_sha256"] = "f" * 64
    marker = _marker_path(root)
    marker.chmod(0o600)
    marker.write_bytes(registry.canonical_json_bytes(tampered))
    marker.chmod(registry.FILE_MODE)

    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_marker_invalid$",
    ) as raised:
        registry._read_for_test(root)
    assert "publication" not in str(raised.value)

    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_authority_invalid$",
    ) as invalid_authority:
        registry._create_or_replay_for_test(
            (tmp_path / "other").resolve(),
            authority_record={"secret": "do-not-echo"},
        )
    assert "do-not-echo" not in str(invalid_authority.value)


def test_file_path_replacement_during_read_is_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _create(root)
    marker = _marker_path(root)
    marker_raw = marker.read_bytes()
    original_read = registry.os.read
    replaced = False

    def replace_then_read(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            marker.rename(tmp_path / "detached-marker")
            marker.write_bytes(marker_raw)
            marker.chmod(registry.FILE_MODE)
        return original_read(descriptor, count)

    monkeypatch.setattr(registry.os, "read", replace_then_read)
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_file_invalid$",
    ):
        registry._read_for_test(root)


def test_registry_path_replacement_during_publication_is_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    replaced = False

    def replace_root(name: str) -> None:
        nonlocal replaced
        if name == "active_pending_directory_fsynced" and not replaced:
            replaced = True
            root.rename(tmp_path / "detached-registry")
            root.mkdir(mode=registry.DIRECTORY_MODE)
            root.chmod(registry.DIRECTORY_MODE)

    monkeypatch.setattr(registry, "_checkpoint", replace_root)
    with pytest.raises(
        registry.ProductionReleaseActiveTransactionError,
        match=r"^release_active_transaction_directory_changed$",
    ):
        _create(root)
