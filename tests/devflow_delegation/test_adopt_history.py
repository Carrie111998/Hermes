"""Historical adoption regression tests.

Legacy DEVFLOW_FIX_REQUEST / DEVFLOW_APPROVAL_REQUEST records must migrate into
DDP at TRIAGED without a mailbox write or EventBus emission. The migration is a
one-time bridge, not an alternate live-emission path.
"""
from __future__ import annotations

import json
from pathlib import Path

from devflow_delegation.adopt_history import (
    adopt,
    gather_approved_keys,
    gather_fix_requests,
)
from devflow_delegation.ledger import DelegationLedger


def _legacy_fix(key: str, *, timestamp: str = "2026-08-04T10:11:12Z") -> dict:
    return {
        "message_id": "legacy-fix-1",
        "idempotency_key": key,
        "protocol_version": "2.0",
        "type": "DEVFLOW_FIX_REQUEST",
        "from": "roadmap-intake",
        "to": "devflow",
        "timestamp": timestamp,
        "payload": {
            "issue": "SR-901",
            "priority": "high",
            "evidence": {
                "roadmap": "roadmap/simplification-roadmap.md",
                "source_row": 42,
                "traces_to": "SR-901",
            },
            "task": "Restore a bounded health query.",
            "recovery": "Revert the bounded-query patch if it regresses.",
            "safety": "No autonomous merge/deploy.",
        },
    }


def _approval(key: str, *, status: str = "AWAITING HUMAN APPROVAL") -> dict:
    return {
        "message_id": "legacy-approval-1",
        "type": "DEVFLOW_APPROVAL_REQUEST",
        "status": status,
        "payload": {
            "source_idempotency_key": key,
            "sr_code": "SR-901",
        },
    }


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_gather_functions_join_top_level_fix_key(monkeypatch, tmp_path):
    """v2 fix idempotency keys live at the envelope top level, while approval
    records place the matching key at payload.source_idempotency_key."""
    import devflow_delegation.adopt_history as history

    monkeypatch.setattr(history, "HERMES_ROOT", tmp_path)
    key = "roadmap:sr-901:v1"
    _write_json(tmp_path / "mailbox/devflow/processed/fix.json", _legacy_fix(key))
    _write_json(tmp_path / "mailbox/main/processed/approval.json", _approval(key))

    assert gather_fix_requests() == {key: _legacy_fix(key)}
    assert set(gather_approved_keys()) == {key}


def test_adopt_moves_matched_history_directly_to_triaged_without_mailbox_write(monkeypatch, tmp_path):
    import devflow_delegation.adopt_history as history

    monkeypatch.setattr(history, "HERMES_ROOT", tmp_path)
    key = "roadmap:sr-901:v1"
    timestamp = "2026-08-04T10:11:12Z"
    _write_json(tmp_path / "mailbox/devflow/processed/fix.json", _legacy_fix(key, timestamp=timestamp))
    _write_json(tmp_path / "mailbox/main/processed/approval.json", _approval(key))

    inbox = tmp_path / "mailbox/devflow/inbox"
    before = list(inbox.glob("*.json")) if inbox.exists() else []
    ledger = DelegationLedger(tmp_path / "devflow/delegation_ledger.db")
    result = adopt(gather_approved_keys(), gather_fix_requests(), ledger)

    assert result == {
        "adopted": 1,
        "triaged": 1,
        "skipped_already_triaged": 0,
        "errors": 0,
    }
    row = ledger.find_by_idempotency_key(key)
    assert row is not None
    assert row["state"] == "TRIAGED"
    # Historical observability should use the original v2 request timestamp,
    # while updated_at records the migration event.
    assert row["created_at"] == timestamp
    assert [t["from_state"] for t in ledger.transitions_for(row["request_id"])] == ["REQUESTED"]
    assert [t["to_state"] for t in ledger.transitions_for(row["request_id"])] == ["TRIAGED"]
    assert [t["actor"] for t in ledger.transitions_for(row["request_id"])] == ["ddp.historical-adoption"]
    assert (list(inbox.glob("*.json")) if inbox.exists() else []) == before
    ledger.close()


def test_adopt_is_idempotent_for_existing_triaged_row(monkeypatch, tmp_path):
    import devflow_delegation.adopt_history as history

    monkeypatch.setattr(history, "HERMES_ROOT", tmp_path)
    key = "roadmap:sr-901:v1"
    _write_json(tmp_path / "mailbox/devflow/processed/fix.json", _legacy_fix(key))
    _write_json(tmp_path / "mailbox/main/processed/approval.json", _approval(key))

    ledger = DelegationLedger(tmp_path / "devflow/delegation_ledger.db")
    first = adopt(gather_approved_keys(), gather_fix_requests(), ledger)
    second = adopt(gather_approved_keys(), gather_fix_requests(), ledger)

    assert first["adopted"] == first["triaged"] == 1
    assert second == {
        "adopted": 0,
        "triaged": 0,
        "skipped_already_triaged": 1,
        "errors": 0,
    }
    assert ledger.summary_counts()["total"] == 1
    ledger.close()


def test_adopt_ignores_approval_without_matching_fix(monkeypatch, tmp_path):
    import devflow_delegation.adopt_history as history

    monkeypatch.setattr(history, "HERMES_ROOT", tmp_path)
    _write_json(
        tmp_path / "mailbox/main/processed/approval.json",
        _approval("roadmap:sr-unknown:v1"),
    )

    ledger = DelegationLedger(tmp_path / "devflow/delegation_ledger.db")
    result = adopt(gather_approved_keys(), gather_fix_requests(), ledger)

    assert result == {
        "adopted": 0,
        "triaged": 0,
        "skipped_already_triaged": 0,
        "errors": 0,
    }
    assert ledger.summary_counts()["total"] == 0
    ledger.close()
