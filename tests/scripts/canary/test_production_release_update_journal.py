from __future__ import annotations

import os
import stat
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch

import pytest

from scripts.canary import production_release_update_journal as journal_module
from scripts.canary import production_release_update_runtime as runtime
from tests.scripts.canary.test_production_release_update_runtime import (
    FakeActions,
    _authority_record as _runtime_authority_record,
    _intent as _runtime_intent,
    _valid_action_receipt,
)


NOW = 1_900_000_000


class SimulatedCrash(BaseException):
    pass


def _intent() -> Mapping[str, Any]:
    return _runtime_intent()


def _authority_record() -> Mapping[str, Any]:
    return _runtime_authority_record()


def _action_receipt(
    intent: Mapping[str, Any],
    phase: str,
    *,
    receipts: Mapping[str, Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    return _valid_action_receipt(
        phase,
        intent=intent,
        receipts=receipts or {},
    )


def _event(
    intent: Mapping[str, Any],
    *,
    sequence: int = 0,
    phase: str = "candidate_validated",
    prior: str = runtime.ZERO_SHA256,
    marker: str = "exact",
    receipts: Mapping[str, Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    return runtime.build_event(
        intent=intent,
        sequence=sequence,
        phase=phase,
        prior_event_sha256=prior,
        receipt=_action_receipt(
            intent,
            phase,
            receipts=receipts,
        ),
        created_at_unix=NOW if marker == "exact" else NOW + 1,
    )


def _configured(
    tmp_path: Path,
) -> tuple[
    journal_module.ReleaseUpdateJournal,
    Mapping[str, Any],
    Path,
]:
    tmp_path.chmod(journal_module.DIRECTORY_MODE)
    intent = _intent()
    transaction = (tmp_path / "transaction").resolve()
    journal = journal_module.ReleaseUpdateJournal._for_test(
        transaction,
        authority_record=_authority_record(),
    )
    return journal, intent, transaction


def _manual_transaction(path: Path) -> None:
    path.mkdir(mode=journal_module.DIRECTORY_MODE, exist_ok=True)
    path.chmod(journal_module.DIRECTORY_MODE)
    os.chown(path, os.geteuid(), path.parent.stat().st_gid)


def test_nonroot_test_mode_pins_owner_private_parent_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    parent_group = tmp_path.stat().st_gid
    effective_group = parent_group + 1
    monkeypatch.setattr(
        journal_module.os,
        "getegid",
        lambda: effective_group,
    )

    journal = journal_module.ReleaseUpdateJournal._for_test(
        (tmp_path / "transaction").resolve(),
        authority_record=_authority_record(),
    )

    assert journal._gid == parent_group
    assert journal.load() == []


def test_production_directory_trust_still_requires_root_group() -> None:
    journal = object.__new__(journal_module.ReleaseUpdateJournal)
    journal._require_root = True
    journal._uid = 0
    journal._gid = 0
    root_directory = {
        "st_mode": stat.S_IFDIR | 0o700,
        "st_uid": 0,
    }

    assert journal._trusted_directory(
        SimpleNamespace(**root_directory, st_gid=0)
    )
    assert not journal._trusted_directory(
        SimpleNamespace(**root_directory, st_gid=1)
    )


def test_append_load_and_exact_replay_are_canonical_and_immutable(
    tmp_path: Path,
) -> None:
    journal, intent, transaction = _configured(tmp_path)
    event = _event(intent)

    assert journal.load() == []
    persisted = journal.append(event)
    replay = journal.append(event)

    assert persisted == event
    assert replay == event
    assert journal.load() == [event]
    entry = transaction / "00000000.json"
    assert entry.read_bytes() == journal_module.canonical_json_bytes(event)
    assert stat.S_IMODE(transaction.stat().st_mode) == 0o700
    assert stat.S_IMODE(entry.stat().st_mode) == 0o400
    assert entry.stat().st_nlink == 1
    assert sorted(path.name for path in transaction.iterdir()) == [
        "00000000.json",
        journal_module.AUTHORITY_FILE_NAME,
    ]


def test_extended_metadata_on_journal_directory_or_event_fails_closed(
    tmp_path: Path,
) -> None:
    directory_root = (tmp_path / "directory-metadata").resolve()
    directory_root.mkdir(mode=0o700)
    directory_root.chmod(0o700)
    directory_journal = journal_module.ReleaseUpdateJournal._for_test(
        directory_root / "transaction",
        authority_record=_authority_record(),
        xattr_reader=lambda descriptor: (
            ("user.injected",)
            if stat.S_ISDIR(os.fstat(descriptor).st_mode)
            else ()
        ),
    )
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="extended_metadata_invalid",
    ):
        directory_journal.load()

    event_root = (tmp_path / "event-metadata").resolve()
    event_root.mkdir(mode=0o700)
    event_root.chmod(0o700)
    metadata_present = False

    def event_reader(descriptor: int) -> tuple[str, ...]:
        if metadata_present and stat.S_ISREG(os.fstat(descriptor).st_mode):
            return ("user.injected",)
        return ()

    event_journal = journal_module.ReleaseUpdateJournal._for_test(
        event_root / "transaction",
        authority_record=_authority_record(),
        xattr_reader=event_reader,
    )
    event_journal.append(_event(_intent()))
    metadata_present = True
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="extended_metadata_invalid",
    ):
        event_journal.load()


def test_authority_header_is_exact_durable_and_reopened_before_event_zero(
    tmp_path: Path,
) -> None:
    journal, _intent_value, transaction = _configured(tmp_path)

    assert journal.load() == []
    header = transaction / journal_module.AUTHORITY_FILE_NAME
    assert header.read_bytes() == journal_module.canonical_json_bytes(
        _authority_record()
    )
    assert stat.S_IMODE(header.stat().st_mode) == 0o400
    assert header.stat().st_nlink == 1

    reopened = journal_module.ReleaseUpdateJournal._for_test(
        transaction,
        authority_record=_authority_record(),
    )
    assert reopened.authority_record == _authority_record()
    assert reopened.load() == []


@pytest.mark.parametrize(
    "boundary",
    journal_module.AUTHORITY_DURABLE_BOUNDARIES,
)
def test_authority_publication_recovers_at_every_durable_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    journal, _intent_value, transaction = _configured(tmp_path)

    def crash(name: str) -> None:
        if name == boundary:
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        journal.load()

    monkeypatch.setattr(journal_module, "_checkpoint", lambda _name: None)
    assert journal.load() == []
    assert (
        transaction / journal_module.AUTHORITY_FILE_NAME
    ).read_bytes() == journal_module.canonical_json_bytes(
        _authority_record()
    )
    assert not (
        transaction / journal_module.AUTHORITY_PENDING_NAME
    ).exists()


@pytest.mark.parametrize(
    "boundary",
    journal_module.AUTHORITY_RECOVERY_DURABLE_BOUNDARIES[2:],
)
def test_linked_authority_recovery_retries_every_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    journal, _intent_value, transaction = _configured(tmp_path)

    def crash_after_link(name: str) -> None:
        if name == "authority_final_linked":
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash_after_link)
    with pytest.raises(SimulatedCrash):
        journal.load()
    pending = transaction / journal_module.AUTHORITY_PENDING_NAME
    final = transaction / journal_module.AUTHORITY_FILE_NAME
    assert pending.stat().st_ino == final.stat().st_ino
    assert pending.stat().st_nlink == 2

    def crash_recovery(name: str) -> None:
        if name == boundary:
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash_recovery)
    with pytest.raises(SimulatedCrash):
        journal.load()

    monkeypatch.setattr(journal_module, "_checkpoint", lambda _name: None)
    assert journal.load() == []
    assert not pending.exists()
    assert final.stat().st_nlink == 1


@pytest.mark.parametrize(
    "boundary",
    journal_module.AUTHORITY_RECOVERY_DURABLE_BOUNDARIES[:2],
)
def test_unpublished_authority_recovery_retries_every_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    journal, _intent_value, transaction = _configured(tmp_path)

    def crash_before_publish(name: str) -> None:
        if name == "authority_pending_directory_fsynced":
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash_before_publish)
    with pytest.raises(SimulatedCrash):
        journal.load()
    assert (
        transaction / journal_module.AUTHORITY_PENDING_NAME
    ).exists()
    assert not (
        transaction / journal_module.AUTHORITY_FILE_NAME
    ).exists()

    def crash_recovery(name: str) -> None:
        if name == boundary:
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash_recovery)
    with pytest.raises(SimulatedCrash):
        journal.load()

    monkeypatch.setattr(journal_module, "_checkpoint", lambda _name: None)
    assert journal.load() == []
    assert (
        transaction / journal_module.AUTHORITY_FILE_NAME
    ).exists()


def test_runtime_executes_against_reopened_file_journal(
    tmp_path: Path,
) -> None:
    journal, intent, transaction = _configured(tmp_path)

    with patch.object(runtime.time, "time", return_value=NOW):
        completed = runtime._execute_update_for_test(
            authority_record=_authority_record(),
            actions=FakeActions(),
            journal=journal,
            lock_factory=nullcontext,
        )
    reopened = journal_module.ReleaseUpdateJournal._for_test(
        transaction,
        authority_record=_authority_record(),
    )

    assert completed.terminal_phase == "completed"
    assert [event["phase"] for event in reopened.load()] == list(
        runtime.FORWARD_PHASES
    )
    assert runtime.load_state(
        intent=intent,
        events=reopened.load(),
    ).terminal_phase == "completed"


@pytest.mark.parametrize(
    "boundary",
    journal_module.DURABLE_BOUNDARIES,
)
def test_append_recovers_exactly_after_every_durable_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    journal, intent, _transaction = _configured(tmp_path)
    event = _event(intent)

    def crash(name: str) -> None:
        if name == boundary:
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        journal.append(event)

    monkeypatch.setattr(journal_module, "_checkpoint", lambda _name: None)
    assert journal.append(event) == event
    assert journal.load() == [event]


@pytest.mark.parametrize(
    "boundary",
    (
        "recovery_final_directory_fsynced",
        "recovery_pending_unlinked",
        "recovery_cleanup_directory_fsynced",
        "recovery_readback_validated",
    ),
)
def test_linked_publication_recovery_retries_every_durable_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    journal, intent, transaction = _configured(tmp_path)
    event = _event(intent)

    def crash_after_link(name: str) -> None:
        if name == "final_linked":
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash_after_link)
    with pytest.raises(SimulatedCrash):
        journal.append(event)
    pending = transaction / ".00000000.pending"
    final = transaction / "00000000.json"
    assert pending.stat().st_ino == final.stat().st_ino
    assert pending.stat().st_nlink == 2

    def crash_recovery(name: str) -> None:
        if name == boundary:
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash_recovery)
    with pytest.raises(SimulatedCrash):
        journal.load()

    monkeypatch.setattr(journal_module, "_checkpoint", lambda _name: None)
    assert journal.load() == [event]
    assert not pending.exists()
    assert final.stat().st_nlink == 1


@pytest.mark.parametrize(
    "boundary",
    (
        "recovery_uncommitted_pending_removed",
        "recovery_uncommitted_pending_cleanup_fsynced",
    ),
)
def test_unpublished_pending_recovery_retries_every_durable_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    journal, intent, transaction = _configured(tmp_path)
    event = _event(intent)

    def crash_before_publish(name: str) -> None:
        if name == "pending_directory_fsynced":
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash_before_publish)
    with pytest.raises(SimulatedCrash):
        journal.append(event)
    assert (transaction / ".00000000.pending").exists()
    assert not (transaction / "00000000.json").exists()

    def crash_recovery(name: str) -> None:
        if name == boundary:
            raise SimulatedCrash

    monkeypatch.setattr(journal_module, "_checkpoint", crash_recovery)
    with pytest.raises(SimulatedCrash):
        journal.load()

    monkeypatch.setattr(journal_module, "_checkpoint", lambda _name: None)
    assert journal.load() == []
    assert journal.append(event) == event


def test_append_rejects_sequence_gap_chain_mismatch_and_divergent_replay(
    tmp_path: Path,
) -> None:
    journal, intent, _transaction = _configured(tmp_path)
    first = _event(intent)
    journal.append(first)

    divergent = _event(intent, marker="different")
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_append_conflict",
    ):
        journal.append(divergent)

    gap = _event(
        intent,
        sequence=2,
        phase="unit_inputs_prepared",
        prior=first["event_sha256"],
    )
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_sequence_invalid",
    ):
        journal.append(gap)

    wrong_chain = _event(
        intent,
        sequence=1,
        phase="prestate_archived",
        prior=runtime.ZERO_SHA256,
    )
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_sequence_invalid",
    ):
        journal.append(wrong_chain)


def test_load_rejects_gap_and_extra_inventory(
    tmp_path: Path,
) -> None:
    journal, intent, transaction = _configured(tmp_path)
    journal.append(_event(intent))
    raw = (transaction / "00000000.json").read_bytes()
    gap = transaction / "00000002.json"
    gap.write_bytes(raw)
    gap.chmod(0o400)

    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_sequence_invalid",
    ):
        journal.load()

    gap.unlink()
    extra = transaction / "notes"
    extra.write_bytes(b"not journal state")
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_inventory_invalid",
    ):
        journal.load()


def test_load_rejects_missing_or_tampered_authority_before_events(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "missing"
    missing_root.mkdir(mode=0o700)
    missing_root.chmod(0o700)
    missing, intent, missing_transaction = _configured(missing_root)
    missing.append(_event(intent))
    (missing_transaction / journal_module.AUTHORITY_FILE_NAME).unlink()

    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_authority_missing",
    ):
        missing.load()

    tampered_root = tmp_path / "tampered"
    tampered_root.mkdir(mode=0o700)
    tampered_root.chmod(0o700)
    tampered, _intent_value, tampered_transaction = _configured(
        tampered_root
    )
    assert tampered.load() == []
    header = tampered_transaction / journal_module.AUTHORITY_FILE_NAME
    header.chmod(0o600)
    header.write_bytes(b'{"tampered":true}')
    header.chmod(0o400)

    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_authority_invalid",
    ):
        tampered.load()


@pytest.mark.parametrize(
    "payload",
    (
        b'{"x":1, "x":2}',
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b"",
        b"{",
    ),
)
def test_load_rejects_noncanonical_duplicate_partial_and_nonfinite_json(
    tmp_path: Path,
    payload: bytes,
) -> None:
    journal, _intent_value, transaction = _configured(tmp_path)
    assert journal.load() == []
    _manual_transaction(transaction)
    entry = transaction / "00000000.json"
    entry.write_bytes(payload)
    entry.chmod(0o400)

    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match=(
            "release_update_journal_(json_invalid|file_invalid)"
        ),
    ):
        journal.load()


def test_load_rejects_symlink_and_special_sequence_nodes(
    tmp_path: Path,
) -> None:
    symlink_root = (tmp_path / "symlink-case").resolve()
    symlink_root.mkdir(mode=0o700)
    symlink_root.chmod(0o700)
    intent = _intent()
    symlink_transaction = symlink_root / "transaction"
    _manual_transaction(symlink_transaction)
    symlink_journal = journal_module.ReleaseUpdateJournal._for_test(
        symlink_transaction,
        authority_record=_authority_record(),
    )
    assert symlink_journal.load() == []
    target = symlink_root / "target"
    target.write_bytes(b"{}")
    (symlink_transaction / "00000000.json").symlink_to(target)
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_file_invalid",
    ):
        symlink_journal.load()

    fifo_root = (tmp_path / "fifo-case").resolve()
    fifo_root.mkdir(mode=0o700)
    fifo_root.chmod(0o700)
    fifo_transaction = fifo_root / "transaction"
    _manual_transaction(fifo_transaction)
    fifo_journal = journal_module.ReleaseUpdateJournal._for_test(
        fifo_transaction,
        authority_record=_authority_record(),
    )
    assert fifo_journal.load() == []
    fifo = fifo_transaction / "00000000.json"
    os.mkfifo(fifo, 0o400)
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_file_invalid",
    ):
        fifo_journal.load()


def test_load_rejects_external_hardlink_to_final_entry(
    tmp_path: Path,
) -> None:
    journal, intent, transaction = _configured(tmp_path)
    journal.append(_event(intent))
    os.link(
        transaction / "00000000.json",
        tmp_path / "external-hardlink",
    )

    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_file_invalid",
    ):
        journal.load()


def test_partial_unpublished_pending_is_discarded_but_suspicious_temp_fails(
    tmp_path: Path,
) -> None:
    journal, intent, transaction = _configured(tmp_path)
    assert journal.load() == []
    _manual_transaction(transaction)
    pending = transaction / ".00000000.pending"
    pending.write_bytes(b'{"partial":')
    pending.chmod(0o400)

    assert journal.load() == []
    assert not pending.exists()

    external = tmp_path / "external"
    external.write_bytes(journal_module.canonical_json_bytes(_event(intent)))
    external.chmod(0o400)
    os.link(external, pending)
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_recovery_invalid",
    ):
        journal.load()


def test_pending_sequence_gap_fails_closed(
    tmp_path: Path,
) -> None:
    journal, intent, transaction = _configured(tmp_path)
    assert journal.load() == []
    _manual_transaction(transaction)
    pending = transaction / ".00000002.pending"
    pending.write_bytes(
        journal_module.canonical_json_bytes(_event(intent))
    )
    pending.chmod(0o400)

    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_recovery_invalid",
    ):
        journal.load()


def test_pending_symlink_and_unknown_temp_name_fail_closed(
    tmp_path: Path,
) -> None:
    first_root = (tmp_path / "pending-symlink").resolve()
    first_root.mkdir(mode=0o700)
    first_root.chmod(0o700)
    intent = _intent()
    first_transaction = first_root / "transaction"
    _manual_transaction(first_transaction)
    first = journal_module.ReleaseUpdateJournal._for_test(
        first_transaction,
        authority_record=_authority_record(),
    )
    assert first.load() == []
    target = first_root / "target"
    target.write_bytes(b"not trusted")
    (first_transaction / ".00000000.pending").symlink_to(target)
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_recovery_invalid",
    ):
        first.load()

    second_root = (tmp_path / "unknown-temp").resolve()
    second_root.mkdir(mode=0o700)
    second_root.chmod(0o700)
    second_transaction = second_root / "transaction"
    _manual_transaction(second_transaction)
    second = journal_module.ReleaseUpdateJournal._for_test(
        second_transaction,
        authority_record=_authority_record(),
    )
    assert second.load() == []
    (second_transaction / ".00000000.other").write_bytes(b"")
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_inventory_invalid",
    ):
        second.load()


def test_transaction_and_ancestor_symlinks_fail_closed(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    intent = _intent()
    target = tmp_path / "target"
    _manual_transaction(target)
    transaction_link = tmp_path / "transaction-link"
    transaction_link.symlink_to(target, target_is_directory=True)
    direct = journal_module.ReleaseUpdateJournal._for_test(
        transaction_link,
        authority_record=_authority_record(),
    )
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_directory_invalid",
    ):
        direct.load()

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    real_parent.chmod(0o700)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    ancestor = journal_module.ReleaseUpdateJournal._for_test(
        parent_link / "transaction",
        authority_record=_authority_record(),
    )
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_directory_invalid",
    ):
        ancestor.load()


def test_transaction_path_swap_is_detected_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    journal, intent, transaction = _configured(tmp_path)
    first = _event(intent)
    journal.append(first)
    second = _event(
        intent,
        sequence=1,
        phase=runtime.FORWARD_PHASES[1],
        prior=first["event_sha256"],
        receipts={"candidate_validated": first["receipt"]},
    )
    moved = tmp_path / "detached-transaction"

    def swap(name: str) -> None:
        if name == "pending_directory_fsynced":
            transaction.rename(moved)
            _manual_transaction(transaction)

    monkeypatch.setattr(journal_module, "_checkpoint", swap)
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_directory_changed",
    ):
        journal.append(second)

    monkeypatch.setattr(journal_module, "_checkpoint", lambda _name: None)
    assert journal.load() == []
    assert (moved / "00000000.json").exists()
    assert (moved / ".00000001.pending").exists()


def test_transaction_parent_path_swap_is_detected_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    parent = tmp_path / "journal-root"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    intent = _intent()
    transaction = parent / "transaction"
    journal = journal_module.ReleaseUpdateJournal._for_test(
        transaction,
        authority_record=_authority_record(),
    )
    moved_parent = tmp_path / "detached-root"

    def swap(name: str) -> None:
        if name == "pending_directory_fsynced":
            parent.rename(moved_parent)
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)

    monkeypatch.setattr(journal_module, "_checkpoint", swap)
    with pytest.raises(
        journal_module.ProductionReleaseUpdateJournalError,
        match="release_update_journal_directory_changed",
    ):
        journal.append(_event(intent))

    monkeypatch.setattr(journal_module, "_checkpoint", lambda _name: None)
    assert journal.load() == []
    assert (moved_parent / "transaction" / ".00000000.pending").exists()


def test_public_production_constructor_exposes_no_path_or_root_override(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        journal_module.ReleaseUpdateJournal(
            (tmp_path / "transaction").resolve(),
            authority_record=_authority_record(),
            require_root=True,
        )
