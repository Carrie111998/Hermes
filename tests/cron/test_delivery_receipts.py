"""Behavior tests for append-only cron delivery receipts."""

from __future__ import annotations

import json


def _receipt(*, execution_id: str, chat_id: str = "123", thread_id: str | None = None) -> dict:
    return {
        "execution_id": execution_id,
        "job_id": "job-1",
        "target": {"platform": "telegram", "chat_id": chat_id, "thread_id": thread_id},
        "state": "accepted",
        "output_sha256": "a" * 64,
    }


def test_receipt_store_appends_once_per_execution_target(tmp_path):
    from cron.delivery_receipts import append_receipt

    path = tmp_path / "delivery-receipts.jsonl"
    first = append_receipt(path, _receipt(execution_id="execution-1"))
    duplicate = append_receipt(path, _receipt(execution_id="execution-1"))

    assert first == {"written": True, "reason": None}
    assert duplicate == {"written": False, "reason": "duplicate_execution_target"}
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["execution_id"] == "execution-1"


def test_receipt_store_allows_distinct_targets_for_same_execution(tmp_path):
    from cron.delivery_receipts import append_receipt

    path = tmp_path / "delivery-receipts.jsonl"
    assert append_receipt(path, _receipt(execution_id="execution-1", chat_id="123"))["written"] is True
    assert append_receipt(path, _receipt(execution_id="execution-1", chat_id="456"))["written"] is True

    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_execution_aggregate_preserves_failure_when_later_target_is_accepted(tmp_path):
    from cron.delivery_receipts import aggregate_execution_state, append_receipt

    path = tmp_path / "delivery-receipts.jsonl"
    failed = _receipt(execution_id="execution-1", chat_id="123")
    failed["state"] = "failed"
    assert append_receipt(path, failed)["written"] is True
    assert append_receipt(path, _receipt(execution_id="execution-1", chat_id="456"))["written"] is True

    assert aggregate_execution_state(path, "execution-1") == "failed"


def test_execution_aggregate_prioritizes_uncertain_over_accepted_regardless_of_target_order(tmp_path):
    from cron.delivery_receipts import aggregate_execution_state, append_receipt

    path = tmp_path / "delivery-receipts.jsonl"
    assert append_receipt(path, _receipt(execution_id="execution-1", chat_id="123"))["written"] is True
    uncertain = _receipt(execution_id="execution-1", chat_id="456")
    uncertain["state"] = "uncertain_in_flight"
    assert append_receipt(path, uncertain)["written"] is True

    assert aggregate_execution_state(path, "execution-1") == "uncertain_in_flight"
